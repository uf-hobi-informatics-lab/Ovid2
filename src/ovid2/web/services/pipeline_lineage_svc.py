"""Field-level lineage of a pipeline package — web tier, static.

Answers *"what happens to column X of table T in pipeline P?"* without
executing pipeline code: for every CDM table, the ordered chain of
functions per output field across the five stages
(``format → deduplicate → transform → map → fix``), plus the
table-wide steps (row filters, dedup, reshapes).

Inputs, all read statically:

* **format** — ``formatters/<t>.yaml`` projections (column refs from
  :func:`ovid2.config.sql_expr_guard.check_expression`), or an opaque
  custom / legacy Python formatter.
* **deduplicate** — the CDM composite key (or the legacy positional key).
* **transform** — ``transforms/assignments.yaml`` → named chains
  (package → user library → shipped) or inline steps.
* **map** — ``mappers/<t>.yaml`` (or the CDM default for legacy ports);
  ``legacy_select`` items become one step per output column.
* **fix** — ``fixer_configs`` chains, or the vendored legacy
  ``fixes`` dicts of a ``legacy_ovid`` port.

Which columns each op touches comes from the *column roles* declared
beside the web-safe catalogues (:mod:`ovid2.config.column_roles`).

Never imports ``ovid2.core`` / ``formatters`` / ``mappers`` /
``transformations`` / ``semantic``
(``tests/contract/test_web_no_pipeline_imports.py``).
"""

from __future__ import annotations

import ast
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ovid2.config.column_roles import ResolvedRoles, resolve_roles

logger = logging.getLogger(__name__)

__all__ = [
    "GROUP_BY",
    "STAGES",
    "FieldLineage",
    "LineageStep",
    "TableLineage",
    "navigator_tree",
    "package_lineage",
    "read_source",
    "table_lineage",
]

#: Pipeline stage order (mirrors ``ovid2.core.loaders`` default stages).
STAGES: tuple[str, ...] = ("format", "deduplicate", "transform", "map", "fix")

#: Axes the navigator can group pipelines by.
GROUP_BY: tuple[str, ...] = ("cdm", "profile", "kind", "template", "cdm_profile")


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class LineageStep:
    """One function applied in one stage.

    Attributes:
        id: Stable id within the table (``<stage>.<n>[.<m>]``).
        stage: One of :data:`STAGES`.
        kind: ``projection`` | ``dedup`` | ``transform`` | ``map_op`` |
            ``legacy_select`` | ``fixer`` | ``legacy_fixer`` | ``opaque``.
        op: Op / fixer / adapter name.
        label: Short human label (``translate_by_dict(demographic_sex_dict)``).
        params: The step's params as declared.
        scope: ``field`` | ``rows`` | ``shape`` | ``table``.
        reads: Columns read.
        writes: Columns written.
        tables: Reference tables read.
        filters_rows: The step also removes rows.
        chain: Transform chain name (named assignments).
        source_doc: Editor document type (``formatter`` / ``assignments`` /
            ``transform`` / ``mapper`` / ``fixers`` / ``file``).
        source_ref: Document ref (``{table}``, ``{name, scope}``, ``{path}``).
        step_path: Path inside the document (``ops[3]``, ``tables.demographic[0].steps[1]``).
        editable: ``False`` for generated / shipped / opaque sources.
        opaque: The step is Python the navigator cannot see into.
        note: One-line remark (e.g. why it is read-only).
    """

    id: str
    stage: str
    kind: str
    op: str
    label: str
    params: dict[str, Any] = field(default_factory=dict)
    scope: str = "field"
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    filters_rows: bool = False
    chain: str = ""
    source_doc: str = ""
    source_ref: dict[str, Any] = field(default_factory=dict)
    step_path: str = ""
    editable: bool = True
    opaque: bool = False
    note: str = ""


@dataclass
class FieldLineage:
    """One CDM field and the step ids that touch it, in stage order."""

    name: str
    rdbms_type: str = ""
    valueset: list[str] = field(default_factory=list)
    is_key: bool = False
    steps: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    status: str = "ok"  # ok | unmapped | constant | opaque


@dataclass
class TableLineage:
    """Full per-field lineage of one table."""

    pipeline: str
    table: str
    cdm: str
    profile: str
    configured: bool
    editable: bool
    fields: list[FieldLineage] = field(default_factory=list)
    steps: list[LineageStep] = field(default_factory=list)
    table_steps: list[str] = field(default_factory=list)
    stage_summary: dict[str, dict[str, Any]] = field(default_factory=dict)
    extra_columns: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _safe_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _first_yaml(directory: Path, stem: str) -> Path | None:
    for suffix in (".yaml", ".yml"):
        path = directory / f"{stem}{suffix}"
        if path.is_file():
            return path
    return None


def _short(value: Any, limit: int = 40) -> str:
    text = value if isinstance(value, str) else yaml.safe_dump(value, default_flow_style=True)
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _expr_columns(expression: str) -> list[str]:
    """Return the column names a Spark-SQL expression references (upper-cased)."""
    from ovid2.config.sql_expr_guard import check_expression

    if not isinstance(expression, str) or not expression.strip():
        return []
    result = check_expression(expression)
    return list(result.columns)


def _step_params(step: dict[str, Any], *drop: str) -> dict[str, Any]:
    return {k: v for k, v in step.items() if k not in drop}


def _upper_index(columns: list[str]) -> dict[str, str]:
    return {c.upper(): c for c in columns}


# ---------------------------------------------------------------------------
# Stage builders
# ---------------------------------------------------------------------------


class _Builder:
    """Accumulates steps for one table and attributes them to fields."""

    def __init__(self, pipeline: str, table: str, columns: list[str], *, editable: bool) -> None:
        self.pipeline = pipeline
        self.table = table
        self.columns = columns
        self.index = _upper_index(columns)
        self.editable = editable
        self.steps: list[LineageStep] = []
        self.counters: dict[str, int] = {}
        self.extra: list[str] = []

    def next_id(self, stage: str) -> str:
        n = self.counters.get(stage, 0)
        self.counters[stage] = n + 1
        return f"{stage}.{n}"

    def canon(self, name: str) -> str:
        """Map a column reference onto the CDM spelling (case-insensitive)."""
        upper = name.upper()
        if upper in self.index:
            return self.index[upper]
        if upper not in self.extra:
            self.extra.append(upper)
        return upper

    def add(self, step: LineageStep) -> LineageStep:
        step.reads = list(dict.fromkeys(self.canon(c) for c in step.reads))
        step.writes = list(dict.fromkeys(self.canon(c) for c in step.writes))
        self.steps.append(step)
        return step


def _apply_roles(step: LineageStep, resolved: ResolvedRoles) -> LineageStep:
    reads = list(resolved.reads)
    for expression in resolved.exprs:
        reads.extend(_expr_columns(expression))
    step.scope = resolved.scope
    step.reads = [c for c in dict.fromkeys(reads) if c not in resolved.writes]
    step.writes = list(resolved.writes)
    step.tables = list(resolved.tables)
    step.filters_rows = resolved.filters_rows
    return step


def _format_steps(b: _Builder, pkg: Path, constants: dict[str, Any], legacy: bool) -> bool:
    """Add format-stage steps; return ``True`` when the table is configured."""
    table = b.table
    fdir = pkg / "formatters"
    path = _first_yaml(fdir, table)
    if path is None and (fdir / f"{table}.py").is_file():
        b.add(
            LineageStep(
                id=b.next_id("format"),
                stage="format",
                kind="opaque",
                op="python",
                label="Python formatter",
                scope="table",
                source_doc="file",
                source_ref={"path": f"formatters/{table}.py"},
                editable=False,
                opaque=True,
                note="Hand-written Python formatter; open the file to see its projection.",
            )
        )
        return True
    if path is not None:
        raw = _safe_yaml(path)
        adapter = str(raw.get("adapter") or "")
        if adapter == "custom":
            class_path = str(raw.get("class") or "")
            rel = _custom_rel(class_path, b.pipeline) or f"formatters/{table}.yaml"
            b.add(
                LineageStep(
                    id=b.next_id("format"),
                    stage="format",
                    kind="opaque",
                    op="custom",
                    label=f"custom ({class_path.rsplit('.', 1)[-1] or 'class'})",
                    params={"class": class_path},
                    scope="table",
                    source_doc="file",
                    source_ref={"path": rel},
                    editable=False,
                    opaque=True,
                    note="Custom formatter class; its projection is Python.",
                )
            )
            return True
        joins = [j for j in raw.get("joins") or [] if isinstance(j, dict)]
        for j_index, join in enumerate(joins):
            b.add(
                LineageStep(
                    id=b.next_id("format"),
                    stage="format",
                    kind="projection",
                    op="join",
                    label=f"join {join.get('with', '')} ({join.get('how', 'left')})",
                    params=dict(join),
                    scope="rows"
                    if join.get("how") in ("inner", "left_semi", "left_anti")
                    else "table",
                    tables=[str(join.get("with", ""))],
                    source_doc="formatter",
                    source_ref={"table": table},
                    step_path=f"joins[{j_index}]",
                    editable=b.editable,
                )
            )
        for name, source in (raw.get("sources") or {}).items():
            if isinstance(source, dict) and source.get("filter"):
                b.add(
                    LineageStep(
                        id=b.next_id("format"),
                        stage="format",
                        kind="projection",
                        op="source_filter",
                        label=f"filter {name}",
                        params={"filter": source["filter"]},
                        scope="rows",
                        filters_rows=True,
                        source_doc="formatter",
                        source_ref={"table": table},
                        step_path=f"sources.{name}.filter",
                        editable=b.editable,
                    )
                )
        for column, expression in (raw.get("projections") or {}).items():
            text = "" if expression is None else str(expression)
            constant = text.strip() in ("", "''", '""') or not _expr_columns(text)
            label = "''" if text.strip() in ("", "''") else _short(text, 36)
            b.add(
                LineageStep(
                    id=b.next_id("format"),
                    stage="format",
                    kind="projection",
                    op="constant" if constant else "project",
                    label=label,
                    params={"expr": text},
                    reads=[],
                    writes=[str(column)],
                    source_doc="formatter",
                    source_ref={"table": table},
                    step_path=f"projections.{column}",
                    editable=b.editable,
                    note=", ".join(_expr_columns(text)),
                )
            )
        for p_index, expression in enumerate(raw.get("post_expressions") or []):
            text = str(expression)
            target = text.rsplit(" as ", 1)[-1].strip() if " as " in text.lower() else ""
            b.add(
                LineageStep(
                    id=b.next_id("format"),
                    stage="format",
                    kind="projection",
                    op="post_expression",
                    label=_short(text, 36),
                    params={"expr": text},
                    writes=[target] if target else [],
                    scope="field" if target else "table",
                    source_doc="formatter",
                    source_ref={"table": table},
                    step_path=f"post_expressions[{p_index}]",
                    editable=b.editable,
                )
            )
        if adapter == "union_of_specs":
            b.add(
                LineageStep(
                    id=b.next_id("format"),
                    stage="format",
                    kind="projection",
                    op="union_of_specs",
                    label="union of "
                    + ", ".join(
                        str(s.get("spec", ""))
                        for s in raw.get("sub_specs") or []
                        if isinstance(s, dict)
                    ),
                    params={"sub_specs": raw.get("sub_specs") or []},
                    scope="shape",
                    source_doc="formatter",
                    source_ref={"table": table},
                    step_path="sub_specs",
                    editable=b.editable,
                )
            )
        return True
    custom = pkg / "formatters_custom" / f"{table}_formatter.py"
    if custom.is_file():
        b.add(
            LineageStep(
                id=b.next_id("format"),
                stage="format",
                kind="opaque",
                op="legacy_formatter" if legacy else "custom",
                label="legacy formatter (generated)" if legacy else "custom formatter",
                scope="table",
                source_doc="file",
                source_ref={"path": f"formatters_custom/{table}_formatter.py"},
                editable=False,
                opaque=True,
                writes=_legacy_formatter_aliases(custom) if legacy else [],
                note=(
                    "Generated legacy-parity port — regenerate, never hand-edit."
                    if legacy
                    else "Custom formatter class; its projection is Python."
                ),
            )
        )
        return True
    return False


def _custom_rel(class_path: str, pipeline: str) -> str:
    prefix = f"ovid2_pipelines.{pipeline}."
    if not class_path.startswith(prefix):
        return ""
    module = class_path[len(prefix) :].rsplit(".", 1)[0]
    return module.replace(".", "/") + ".py"


def _legacy_formatter_aliases(path: Path) -> list[str]:
    """Return the ``.alias('X')`` output names of a generated legacy formatter."""
    import re

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return list(dict.fromkeys(re.findall(r"\.alias\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]\s*\)", text)))


def _dedup_step(b: _Builder, handle: Any, legacy: bool, constants: dict[str, Any]) -> None:
    key: tuple[str, ...] = ()
    note = ""
    if legacy:
        from ovid2.legacy.dedup import legacy_dedup_key

        whole = frozenset(str(t).lower() for t in constants.get("LEGACY_DEDUP_WHOLE_ROW") or ())
        skip = frozenset(str(t).lower() for t in constants.get("LEGACY_DEDUP_SKIP") or ())
        found = legacy_dedup_key(b.table, b.columns, whole_row_tables=whole, skip_tables=skip)
        if found is None:
            return
        key = found
        note = "legacy positional key"
    elif handle is not None:
        try:
            key = tuple(handle.dedup_key_for(b.table))
        except ValueError:
            key = ()
        note = "CDM composite key"
    whole_row = key in ((), ("*",))
    b.add(
        LineageStep(
            id=b.next_id("deduplicate"),
            stage="deduplicate",
            kind="dedup",
            op="deduplicate",
            label="whole-row dedup" if whole_row else "dedup on " + ", ".join(key),
            params={"key": list(key)},
            scope="rows",
            reads=[] if whole_row else list(key),
            filters_rows=True,
            source_doc="",
            editable=False,
            note=note,
        )
    )


def _transform_step(
    b: _Builder,
    step: dict[str, Any],
    *,
    chain: str,
    source_doc: str,
    source_ref: dict[str, Any],
    step_path: str,
    editable: bool,
    stage: str = "transform",
    parent_id: str = "",
) -> None:
    from ovid2.config.transforms_catalogue import TRANSFORM_COLUMN_ROLES

    op = str(step.get("op", ""))
    params = _step_params(step, "op")
    role = TRANSFORM_COLUMN_ROLES.get(op)
    known = role is not None
    resolved = resolve_roles(role, params, table=b.table)
    label = op
    if op == "derive_column":
        label = f"derive {params.get('column', '')} = {_short(params.get('expr', ''), 28)}"
    elif op == "filter_expr":
        label = f"filter {_short(params.get('expr', ''), 30)}"
    sid = f"{parent_id}.{b.counters.get(parent_id, 0)}" if parent_id else b.next_id(stage)
    if parent_id:
        b.counters[parent_id] = b.counters.get(parent_id, 0) + 1
    b.add(
        _apply_roles(
            LineageStep(
                id=sid,
                stage=stage,
                kind="transform",
                op=op,
                label=label,
                params=params,
                chain=chain,
                source_doc=source_doc,
                source_ref=source_ref,
                step_path=step_path,
                editable=editable,
                note="" if known else "unknown transform op",
            ),
            resolved,
        )
    )


def _find_chain(name: str, pkg: Path) -> tuple[dict[str, Any], str, Path | None]:
    """Return ``(chain_doc, scope, path)`` — scope ``pipeline`` / ``user`` / ``shipped``."""
    import ovid2
    from ovid2.config.transform_assignments import user_library_dir

    shipped = Path(ovid2.__file__).resolve().parent / "resources" / "transforms"
    for scope, directory in (
        ("pipeline", pkg / "transforms"),
        ("user", user_library_dir()),
        ("shipped", shipped),
    ):
        path = _first_yaml(directory, name)
        if path is not None:
            return _safe_yaml(path), scope, path
    py = pkg / "transforms" / f"{name}.py"
    if py.is_file():
        return {"steps": _python_spec_steps(py)}, "pipeline_py", py
    return {}, "", None


def _python_spec_steps(path: Path) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "SPEC" for t in node.targets
        ):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                return []
            steps = value.get("steps") if isinstance(value, dict) else None
            return [s for s in steps or [] if isinstance(s, dict)]
    return []


def _transform_steps(b: _Builder, pkg: Path, assigned: dict[str, list[Any]], legacy: bool) -> None:
    for key in ("*", b.table):
        for a_index, assignment in enumerate(assigned.get(key, [])):
            if not assignment.enabled:
                continue
            base_path = f"tables.{key}[{a_index}]"
            if assignment.spec:
                chain_doc, scope, path = _find_chain(assignment.spec, pkg)
                if path is None:
                    b.add(
                        LineageStep(
                            id=b.next_id("transform"),
                            stage="transform",
                            kind="transform",
                            op="missing_chain",
                            label=f"{assignment.spec} (missing)",
                            scope="table",
                            chain=assignment.spec,
                            source_doc="assignments",
                            source_ref={"table": key},
                            step_path=base_path,
                            editable=not legacy,
                            note="assigned chain not found in package, user or shipped library",
                        )
                    )
                    continue
                if chain_doc.get("enabled") is False:
                    continue
                editable = scope in ("pipeline", "user") and not legacy
                for s_index, step in enumerate(chain_doc.get("steps") or []):
                    if isinstance(step, dict):
                        _transform_step(
                            b,
                            step,
                            chain=assignment.spec,
                            source_doc="transform",
                            source_ref={"name": assignment.spec, "scope": scope},
                            step_path=f"steps[{s_index}]",
                            editable=editable,
                        )
            else:
                for s_index, step in enumerate(assignment.steps or ()):
                    if isinstance(step, dict):
                        _transform_step(
                            b,
                            step,
                            chain="",
                            source_doc="assignments",
                            source_ref={"table": key},
                            step_path=f"{base_path}.steps[{s_index}]",
                            editable=not legacy,
                        )


def _legacy_select_expr(expr: Any) -> tuple[str, list[str], str]:
    """Return ``(op, reads, label)`` for one ``legacy_select`` item expression."""
    if not isinstance(expr, dict):
        return "col", [], ""
    if "encrypt" in expr:
        return "encrypt", [str(expr["encrypt"])], "encrypt_id"
    if "dict" in expr:
        return "dict", [str(expr.get("col", ""))], f"dict {expr['dict']}"
    if "dict_only" in expr:
        return "dict", [str(expr.get("col", ""))], f"dict {expr['dict_only']}"
    if "coalesce" in expr:
        reads: list[str] = []
        for sub in expr.get("coalesce") or []:
            if isinstance(sub, dict):
                for k in ("col", "nonblank", "encrypt"):
                    if isinstance(sub.get(k), str):
                        reads.append(str(sub[k]))
        return "coalesce", reads, "coalesce"
    if "udf" in expr:
        args = [str(a) for a in expr.get("args") or [] if isinstance(a, str)]
        return "udf", args, f"udf {expr['udf']}"
    if "lit" in expr:
        return "literal", [], f"lit {_short(expr['lit'], 16)}"
    if expr.get("updated"):
        return "literal", [], "current time"
    if expr.get("source"):
        return "literal", [], "source name"
    if "col" in expr:
        cast = f"::{expr['cast']}" if "cast" in expr else ""
        return "col", [str(expr["col"])], f"col {expr['col']}{cast}"
    return "col", [], _short(expr, 24)


def _map_steps(
    b: _Builder,
    pkg: Path,
    cdm: str,
    legacy: bool,
    mappers_type: str,
) -> str:
    """Add map-stage steps; return the mapper source (``yaml`` / ``cdm_default`` / …)."""
    from ovid2.config.mapper_ops_catalogue import MAPPER_OP_COLUMN_ROLES

    path = _first_yaml(pkg / "mappers", b.table)
    source = "yaml" if path is not None else ""
    editable = b.editable
    if path is None and legacy:
        import ovid2

        cdm_dir = Path(ovid2.__file__).resolve().parent / "resources" / "cdms" / cdm / "mappers"
        candidate = _first_yaml(cdm_dir, b.table)
        if candidate is not None:
            path, source, editable = candidate, "cdm_default", False
    if path is None:
        if mappers_type:
            b.add(
                LineageStep(
                    id=b.next_id("map"),
                    stage="map",
                    kind="opaque",
                    op="legacy_class",
                    label=f"{mappers_type} mapper",
                    scope="table",
                    editable=False,
                    opaque=True,
                    note="Legacy mapper class (no declarative spec).",
                )
            )
            return "legacy_class"
        return ""
    raw = _safe_yaml(path)
    ref = {"table": b.table} if source == "yaml" else {"path": f"cdm:{cdm}/mappers/{path.name}"}
    doc = "mapper" if source == "yaml" else "cdm_mapper"
    note = "" if source == "yaml" else "CDM default mapper (generated) — read-only."
    for o_index, op_entry in enumerate(raw.get("ops") or []):
        if not isinstance(op_entry, dict):
            continue
        op = str(op_entry.get("op", ""))
        params = _step_params(op_entry, "op")
        if op == "legacy_select":
            for c_index, item in enumerate(params.get("columns") or []):
                if isinstance(item, str):
                    out, expr = item, {"col": item}
                elif isinstance(item, dict) and len(item) == 1:
                    out, expr = next(iter(item.items()))
                else:
                    continue
                kind_op, reads, label = _legacy_select_expr(expr)
                if kind_op == "col" and reads == [out]:
                    label = "keep"
                b.add(
                    LineageStep(
                        id=f"map.{o_index}.{c_index}",
                        stage="map",
                        kind="legacy_select",
                        op=kind_op,
                        label=label or kind_op,
                        params={str(out): expr} if not isinstance(item, str) else {},
                        reads=[r for r in reads if r and r.upper() != str(out).upper()],
                        writes=[str(out)],
                        source_doc=doc,
                        source_ref=ref,
                        step_path=f"ops[{o_index}].columns[{c_index}]",
                        editable=editable,
                        note=note,
                    )
                )
            append = params.get("append_additional_fields")
            if append:
                b.add(
                    LineageStep(
                        id=f"map.{o_index}.append",
                        stage="map",
                        kind="legacy_select",
                        op="append_additional_fields",
                        label="append extra input columns",
                        params={"append_additional_fields": append},
                        scope="shape",
                        source_doc=doc,
                        source_ref=ref,
                        step_path=f"ops[{o_index}].append_additional_fields",
                        editable=editable,
                        note=note,
                    )
                )
            b.counters["map"] = max(b.counters.get("map", 0), o_index + 1)
            continue
        if op == "apply_transforms":
            parent = f"map.{o_index}"
            b.counters["map"] = max(b.counters.get("map", 0), o_index + 1)
            steps = params.get("steps")
            chain = str(params.get("spec") or "")
            chain_ref: dict[str, Any] = ref
            chain_doc_type = doc
            chain_editable = editable
            if chain and not steps:
                chain_doc, scope, _path = _find_chain(chain, pkg)
                steps = chain_doc.get("steps") or []
                chain_ref = {"name": chain, "scope": scope}
                chain_doc_type = "transform"
                chain_editable = scope in ("pipeline", "user") and not legacy
            for s_index, step in enumerate(steps or []):
                if isinstance(step, dict):
                    _transform_step(
                        b,
                        step,
                        chain=chain,
                        source_doc=chain_doc_type,
                        source_ref=chain_ref,
                        step_path=(
                            f"steps[{s_index}]"
                            if chain_doc_type == "transform"
                            else f"ops[{o_index}].steps[{s_index}]"
                        ),
                        editable=chain_editable,
                        stage="map",
                        parent_id=parent,
                    )
            continue
        role = MAPPER_OP_COLUMN_ROLES.get(op)
        label = op
        if op == "translate_by_dict":
            label = f"translate_by_dict({params.get('dict', '')})"
        elif op == "translate_by_concept":
            label = f"translate_by_concept({params.get('vocabulary', '')})"
        elif op == "case_map":
            label = f"case_map({params.get('mode', 'upper')})"
        elif op == "literal":
            label = f"literal {_short(params.get('value', ''), 16)}"
        elif op == "hash_id":
            label = f"hash_id({params.get('salt_key', 'id_salt')})"
        step = LineageStep(
            id=f"map.{o_index}",
            stage="map",
            kind="map_op",
            op=op,
            label=label,
            params=params,
            source_doc=doc,
            source_ref=ref,
            step_path=f"ops[{o_index}]",
            editable=editable,
            note=note if role is not None else "unknown mapper op",
        )
        b.counters["map"] = max(b.counters.get("map", 0), o_index + 1)
        b.add(_apply_roles(step, resolve_roles(role, params, table=b.table)))
    return source


def _fix_entry_step(
    b: _Builder,
    entry: dict[str, Any],
    *,
    index: int,
    source_doc: str,
    source_ref: dict[str, Any],
    step_path: str,
    editable: bool,
    kind: str = "fixer",
    note: str = "",
    native: str | None = None,
) -> None:
    from ovid2.config.fixers_catalogue import FIXER_CATALOGUE, FIXER_COLUMN_ROLES

    name = native or str(entry.get("fix", ""))
    params = {k: v for k, v in entry.items() if k not in ("fix",)}
    normalised = dict(params)
    for named, positional in (FIXER_CATALOGUE.get(name) or {}).get("aliases", {}).items():
        if named in normalised and positional not in normalised:
            normalised[positional] = normalised[named]
    role = FIXER_COLUMN_ROLES.get(name)
    resolved = resolve_roles(role, normalised, table=b.table, literals=True)
    shown = str(entry.get("fix", name)) if kind == "legacy_fixer" else name
    args = [str(normalised.get(s, "")) for s in ("src1", "src2") if normalised.get(s)]
    args = [a for a in args if a.lower() not in ("notused", "not_used")]
    label = shown + (f"({', '.join(_short(a, 18) for a in args)})" if args else "")
    if role is None and not note:
        note = "fixer is not in the Ovid2 catalogue — columns unknown"
    step = LineageStep(
        id=f"fix.{index}",
        stage="fix",
        kind=kind,
        op=shown,
        label=label,
        params=params,
        source_doc=source_doc,
        source_ref=source_ref,
        step_path=step_path,
        editable=editable,
        opaque=role is None,
        note=note,
    )
    b.add(_apply_roles(step, resolved))


def _legacy_table_name(value: str) -> str:
    """``mapped_demographic.csv`` → ``demographic``."""
    text = value.strip()
    for prefix in ("mapped_", "fixed_", "formatted_", "deduplicated_"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text.removesuffix(".csv")


def _fix_steps(
    b: _Builder,
    pipeline: str,
    constants: dict[str, Any],
    chains: dict[str, list[dict[str, Any]]],
    legacy: bool,
    vendor_dir: Path | None,
) -> None:
    from ovid2.config.fixers_catalogue import FIXER_COLUMN_ROLES, native_fix_name

    if not legacy:
        doc_ref = {"table": b.table}
        for index, entry in enumerate(chains.get(b.table, [])):
            _fix_entry_step(
                b,
                entry,
                index=index,
                source_doc="fixers",
                source_ref=doc_ref,
                step_path=f"fixer_chains.{b.table}[{index}]",
                editable=b.editable,
            )
        return
    if vendor_dir is None:
        return
    script = vendor_dir / f"{b.table}_fixer.py"
    if not script.is_file():
        return
    from ovid2.legacy.fix_stage import read_fixes

    try:
        entries = read_fixes(script)
    except (OSError, SyntaxError, ValueError, KeyError):
        b.add(
            LineageStep(
                id=b.next_id("fix"),
                stage="fix",
                kind="opaque",
                op="legacy_fixer_script",
                label="legacy fixer script (unreadable)",
                scope="table",
                editable=False,
                opaque=True,
            )
        )
        return
    rel = str(script.relative_to(vendor_dir.parent.parent.parent))
    for index, legacy_entry in enumerate(entries):
        native = (
            native_fix_name(legacy_entry.fix_name) if legacy_entry.fix_type == "common" else None
        )
        params = {
            "fix_type": legacy_entry.fix_type,
            "version": legacy_entry.version,
            "src1": legacy_entry.src1,
            "src2": legacy_entry.src2,
        }
        resolved_entry: dict[str, Any] = {"fix": legacy_entry.fix_name, **params}
        if native and native in FIXER_COLUMN_ROLES:
            for slot in ("src1", "src2"):
                value = str(resolved_entry.get(slot, ""))
                if value.endswith(".csv"):
                    resolved_entry[slot] = _legacy_table_name(value)
        _fix_entry_step(
            b,
            resolved_entry,
            index=index,
            source_doc="vendor_file",
            source_ref={"path": rel},
            step_path=f"fixes[{index + 1}]",
            editable=False,
            kind="legacy_fixer",
            native=native if legacy_entry.fix_type == "common" else None,
            note=(
                "Vendored legacy fixer — pinned by MANIFEST.json."
                if legacy_entry.fix_type == "common"
                else "Partner custom legacy fix (Python)."
            ),
        )


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


class _Context:
    """Per-package facts shared by every table."""

    def __init__(self, pipeline: str) -> None:
        from ovid2.config.fixer_configs import FixerConfigsError, load_fixer_configs
        from ovid2.config.transform_assignments import AssignmentsError, load_assignments
        from ovid2.tools.pipeline_identity import read_settings_constants
        from ovid2.web.services.pipeline_inspect_svc import _package_dir, _settings_text

        pkg = _package_dir(pipeline)
        if pkg is None:
            raise LookupError(f"Pipeline package '{pipeline}' is not installed.")
        self.pipeline = pipeline
        self.pkg = pkg
        self.constants = read_settings_constants(_settings_text(pkg))
        self.cdm = str(self.constants.get("cdm") or "")
        if not self.cdm:
            from ovid2.cdm.registry import resolve_cdm_for_pipeline

            try:
                self.cdm = resolve_cdm_for_pipeline(pipeline).name
            except ValueError:
                self.cdm = ""
        self.profile = str(self.constants.get("ETL_PROFILE") or "default")
        self.legacy = self.profile == "legacy_ovid"
        self.mappers_type = str(self.constants.get("mappers_type") or "")
        self.handle: Any = None
        if self.cdm:
            from ovid2.cdm.registry import get_cdm

            try:
                self.handle = get_cdm(self.cdm)
            except (KeyError, ValueError):
                self.handle = None
        self.warnings: list[str] = []
        try:
            self.chains, self.multi, self.fixer_source = load_fixer_configs(pipeline)
        except FixerConfigsError as exc:
            self.chains, self.multi, self.fixer_source = {}, [], "invalid"
            self.warnings.append(str(exc))
        try:
            self.assigned: dict[str, list[Any]] = dict(load_assignments(pipeline))
        except (AssignmentsError, yaml.YAMLError) as exc:
            self.assigned = {}
            self.warnings.append(str(exc))
        self.vendor_dir: Path | None = None
        if self.legacy:
            partner = str(self.constants.get("LEGACY_VENDOR_PARTNER") or pipeline)
            candidate = (
                pkg.parent.parent / "legacy_ovid_vendor" / "partners" / partner / "fixer_scripts"
            )
            self.vendor_dir = candidate if candidate.is_dir() else None

    def tables(self) -> list[str]:
        return list(self.handle.tables()) if self.handle is not None else []

    def fields(self, table: str) -> list[tuple[str, str]]:
        if self.handle is None:
            return []
        try:
            return [(f.name, f.rdbms_type) for f in self.handle.load_table_fields(table)]
        except (KeyError, ValueError, OSError):
            return []


def _build_table(ctx: _Context, table: str) -> TableLineage:
    fields = ctx.fields(table)
    columns = [name for name, _ in fields]
    b = _Builder(ctx.pipeline, table, columns, editable=not ctx.legacy)
    configured = _format_steps(b, ctx.pkg, ctx.constants, ctx.legacy)
    if configured:
        _dedup_step(b, ctx.handle, ctx.legacy, ctx.constants)
    _transform_steps(b, ctx.pkg, ctx.assigned, ctx.legacy)
    map_source = _map_steps(b, ctx.pkg, ctx.cdm, ctx.legacy, ctx.mappers_type)
    _fix_steps(b, ctx.pipeline, ctx.constants, ctx.chains, ctx.legacy, ctx.vendor_dir)

    keys: set[str] = set()
    if ctx.handle is not None:
        try:
            keys = set(ctx.handle.dedup_key_for(table))
        except ValueError:
            keys = set()
    valuesets = ctx.handle.valuesets.get(table, {}) if ctx.handle is not None else {}

    out_fields: list[FieldLineage] = []
    by_name: dict[str, FieldLineage] = {}
    for name, rdbms_type in fields:
        row = FieldLineage(
            name=name,
            rdbms_type=rdbms_type,
            valueset=list(valuesets.get(name) or ()),
            is_key=name in keys,
        )
        out_fields.append(row)
        by_name[name] = row
    for extra in b.extra:
        if extra not in by_name:
            row = FieldLineage(name=extra, status="extra")
            out_fields.append(row)
            by_name[extra] = row

    table_steps: list[str] = []
    format_opaque = any(s.stage == "format" and s.opaque for s in b.steps)
    for step in b.steps:
        touched = [*step.writes, *step.reads]
        if step.scope != "field" or not touched:
            table_steps.append(step.id)
        for column in dict.fromkeys(touched):
            target = by_name.get(column)
            if target is not None and step.id not in target.steps:
                target.steps.append(step.id)
        if step.kind == "projection" and step.op in ("project", "constant"):
            for column in step.writes:
                target = by_name.get(column)
                if target is not None:
                    target.sources = _expr_columns(str(step.params.get("expr", "")))
    format_written = {c for s in b.steps if s.stage == "format" for c in s.writes}
    for row in out_fields:
        if row.status == "extra":
            continue
        fmt = [s for s in b.steps if s.stage == "format" and row.name in s.writes]
        later = [s for s in b.steps if s.stage != "format" and row.name in s.writes]
        if fmt and all(s.op == "constant" for s in fmt) and not later:
            row.status = "constant"
        elif not fmt and format_opaque:
            row.status = "opaque"
        elif configured and row.name not in format_written and not format_opaque:
            row.status = "unmapped"

    summary: dict[str, dict[str, Any]] = {}
    for stage in STAGES:
        stage_steps = [s for s in b.steps if s.stage == stage]
        summary[stage] = {
            "count": len(stage_steps),
            "opaque": any(s.opaque for s in stage_steps),
            "editable": any(s.editable for s in stage_steps),
        }
    summary["map"]["source"] = map_source
    summary["fix"]["source"] = "legacy_vendor" if ctx.legacy else ctx.fixer_source
    return TableLineage(
        pipeline=ctx.pipeline,
        table=table,
        cdm=ctx.cdm,
        profile=ctx.profile,
        configured=configured,
        editable=not ctx.legacy,
        fields=out_fields,
        steps=b.steps,
        table_steps=table_steps,
        stage_summary=summary,
        extra_columns=list(b.extra),
        warnings=list(ctx.warnings),
    )


def table_lineage(pipeline: str, table: str) -> TableLineage:
    """Return the full per-field lineage of one table.

    Raises:
        LookupError: Unknown package or table.
    """
    ctx = _Context(pipeline)
    if table not in ctx.tables():
        raise LookupError(f"'{table}' is not a {ctx.cdm or 'CDM'} table of pipeline '{pipeline}'.")
    return _build_table(ctx, table)


def package_lineage(pipeline: str) -> dict[str, Any]:
    """Return a per-table lineage *summary* for every CDM table of a package.

    Each table row carries its stage summary, field count, the steps'
    ops per stage and the fields' statuses — enough to draw the
    navigator's tree and overview graph without per-table fetches.

    Raises:
        LookupError: Unknown package.
    """
    ctx = _Context(pipeline)
    rows: list[dict[str, Any]] = []
    for table in ctx.tables():
        lineage = _build_table(ctx, table)
        statuses: dict[str, int] = {}
        for row in lineage.fields:
            statuses[row.status] = statuses.get(row.status, 0) + 1
        rows.append(
            {
                "table": table,
                "configured": lineage.configured,
                "fields": len(lineage.fields),
                "statuses": statuses,
                "stages": lineage.stage_summary,
                "ops": {
                    stage: sorted({s.op for s in lineage.steps if s.stage == stage})
                    for stage in STAGES
                },
                "chains": sorted({s.chain for s in lineage.steps if s.chain}),
                "has_udf": any(
                    s.kind == "transform" and s.op in ("derive_column", "filter_expr")
                    for s in lineage.steps
                ),
            }
        )
    return {
        "pipeline": pipeline,
        "cdm": ctx.cdm,
        "profile": ctx.profile,
        "editable": not ctx.legacy,
        "multi_table_fixes": [str(e.get("fix", "")) for e in ctx.multi],
        "tables": rows,
        "warnings": ctx.warnings,
    }


# ---------------------------------------------------------------------------
# Read-only sources outside the package
# ---------------------------------------------------------------------------

_MAX_SOURCE_BYTES = 512 * 1024


def read_source(pipeline: str, ref_path: str) -> dict[str, Any]:
    """Return the text of a lineage ``source_ref.path`` that lives outside the package.

    Accepts exactly the two shapes the lineage emits:

    * ``cdm:<cdm>/mappers/<table>.yaml`` — a shipped CDM default mapper;
    * ``partners/<partner>/(fixer_scripts|custom_fixes)/…`` — a vendored
      legacy file of the package's own vendor partner.

    Raises:
        LookupError: Unknown package, a path outside those roots, or a
            missing file.
    """
    import ovid2

    ctx = _Context(pipeline)
    if ref_path.startswith("cdm:"):
        cdm, _, rest = ref_path[4:].partition("/")
        if cdm != ctx.cdm or not rest.startswith("mappers/"):
            raise LookupError("Not a mapper of this pipeline's CDM.")
        root = Path(ovid2.__file__).resolve().parent / "resources" / "cdms" / cdm
        target = (root / rest).resolve()
    else:
        if not ctx.legacy or ctx.vendor_dir is None:
            raise LookupError("Only legacy_ovid ports have vendored sources.")
        partner_dir = ctx.vendor_dir.parent
        root = partner_dir.parent.parent  # legacy_ovid_vendor/
        target = (root / ref_path).resolve()
        if not str(target).startswith(str(partner_dir.resolve()) + "/"):
            raise LookupError("Path is outside the package's vendor partner.")
    if not str(target).startswith(str(root.resolve()) + "/") or target.suffix not in (
        ".py",
        ".yaml",
        ".yml",
    ):
        raise LookupError("Path not permitted.")
    if not target.is_file():
        raise LookupError("File not found.")
    data = target.read_bytes()[:_MAX_SOURCE_BYTES]
    return {
        "path": ref_path,
        "text": data.decode("utf-8", errors="replace"),
        "readonly": True,
        "truncated": target.stat().st_size > _MAX_SOURCE_BYTES,
    }


# ---------------------------------------------------------------------------
# Navigator tree (pipelines grouped by a switchable axis)
# ---------------------------------------------------------------------------


def _package_facts(pipeline: str) -> dict[str, Any]:
    from ovid2.tools.pipeline_identity import read_settings_constants
    from ovid2.web.services.pipeline_inspect_svc import _package_dir, _settings_text

    pkg = _package_dir(pipeline)
    if pkg is None:
        return {}
    constants = read_settings_constants(_settings_text(pkg))
    profile = str(constants.get("ETL_PROFILE") or "default")
    cdm = str(constants.get("cdm") or "")
    if not cdm:
        from ovid2.cdm.registry import resolve_cdm_for_pipeline

        try:
            cdm = resolve_cdm_for_pipeline(pipeline).name
        except ValueError:
            cdm = ""
    formatted = {
        p.stem
        for p in (pkg / "formatters").glob("*")
        if p.suffix in (".yaml", ".yml", ".py") and not p.name.startswith("_")
    } | {
        p.name.removesuffix("_formatter.py")
        for p in (pkg / "formatters_custom").glob("*_formatter.py")
    }
    return {
        "pipeline": pipeline,
        "cdm": cdm,
        "profile": profile,
        "template": str(constants.get("LEGACY_VENDOR_PARTNER") or ""),
        "has_transforms": (pkg / "transforms" / "assignments.yaml").is_file(),
        "has_mappers": any((pkg / "mappers").glob("*.y*ml")),
        "fixer_source": (
            "yaml"
            if (pkg / "fixer_configs.yaml").is_file()
            else ("python" if (pkg / "fixer_configs.py").is_file() else "")
        ),
        "formatted_tables": len(formatted),
        "editable": profile != "legacy_ovid",
    }


def navigator_tree(group_by: str = "cdm") -> dict[str, Any]:
    """Return every installed package grouped by ``group_by``.

    Args:
        group_by: One of :data:`GROUP_BY`. ``kind`` groups by the run
            kinds of the pipeline *configs* that use the package
            (``unused`` when none); ``template`` by the template id the
            package is, or was cloned from.

    Raises:
        ValueError: Unknown ``group_by``.
    """
    if group_by not in GROUP_BY:
        raise ValueError(f"group_by must be one of {', '.join(GROUP_BY)}")
    from ovid2.tools.templates import TEMPLATES
    from ovid2.web.services import pipelines_meta_svc, pipelines_svc

    by_source = {t.source_pipeline: t.id for t in TEMPLATES.values()}
    configs: dict[str, list[dict[str, Any]]] = {}
    try:
        for cfg in pipelines_svc.list_pipelines():
            configs.setdefault(cfg.pipeline, []).append(
                {"name": cfg.name, "kind": cfg.kind, "enabled": cfg.enabled}
            )
    except (OSError, ValueError) as exc:  # pragma: no cover — config dir unreadable
        logger.warning("navigator: cannot list pipeline configs: %s", exc)

    packages: list[dict[str, Any]] = []
    for name in pipelines_meta_svc.discover_pipelines():
        facts = _package_facts(name)
        if not facts:
            continue
        facts["configs"] = configs.get(name, [])
        facts["template"] = (
            by_source.get(name)
            or by_source.get(facts["template"], "")
            or ("legacy_port" if facts["profile"] == "legacy_ovid" else "custom")
        )
        packages.append(facts)

    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    keys: list[tuple[str, ...]]
    for pkg in packages:
        if group_by == "kind":
            kinds = sorted({c["kind"] for c in pkg["configs"]}) or ["unused"]
            keys = [(k,) for k in kinds]
        elif group_by == "cdm_profile":
            keys = [(pkg["cdm"] or "unknown", pkg["profile"])]
        else:
            keys = [(str(pkg[group_by]) or "unknown",)]
        for key in keys:
            groups.setdefault(key, []).append(pkg)

    out_groups: list[dict[str, Any]] = []
    for key in sorted(groups):
        out_groups.append(
            {
                "key": list(key),
                "label": " · ".join(key),
                "pipelines": sorted(groups[key], key=lambda p: p["pipeline"]),
            }
        )
    return {"group_by": group_by, "group_by_choices": list(GROUP_BY), "groups": out_groups}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("This module is imported by the ovid2 web routes; it is not a script.")
