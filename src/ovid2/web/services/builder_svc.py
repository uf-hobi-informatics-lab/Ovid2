"""Pipeline builder service — edit a package's specs + the transform library.

Backs the web pipeline builder (``/pipeline-packages/{p}/builder``), the
transform library page (``/transforms``), the JSON API and the MCP tools.
Every write is validated *before* it touches disk, written atomically
(tmp + rename, mode 0644) and audited with before/after hashes — the same
discipline as :mod:`formatters_svc` / :mod:`mappers_svc`.

Editable documents:

* **Transform library** (``$XDG_CONFIG_HOME/ovid2/transforms/<name>.yaml``)
  — the user's reusable "UDF" chains.
* **Pipeline transforms** (``<pkg>/transforms/<name>.yaml``) — chains
  private to one package (override the library).
* **Transform assignments** (``<pkg>/transforms/assignments.yaml``) —
  which chains the ``transform`` stage runs on which table.
* **Fixer chains** (``<pkg>/fixer_configs.yaml``) — preferred over
  ``fixer_configs.py`` at run time.
* **Settings** (``<pkg>/pipeline_settings.py``) — *literal constants only*,
  rewritten in place with :func:`ovid2.tools.pipeline_identity.rewrite_settings`
  (computed values stay read-only).

Validation is static and web-safe: JSON schemas, the web-safe transform /
fixer catalogues, per-op ``params_schema`` checks and the SQL expression
allow-list (:mod:`ovid2.config.sql_expr_guard`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from ovid2.web import db as web_db

logger = logging.getLogger(__name__)

__all__ = [
    "BuilderError",
    "assign_chain",
    "delete_library_transform",
    "delete_pipeline_transform",
    "get_assignments",
    "get_fixers",
    "get_library_transform",
    "get_pipeline_transform",
    "get_settings",
    "list_library_transforms",
    "list_pipeline_transforms",
    "save_assignments",
    "save_fixers",
    "save_library_transform",
    "save_pipeline_transform",
    "save_settings",
    "validate_assignments",
    "validate_document",
    "validate_fixers",
    "validate_transform",
]

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_RESERVED = frozenset({"assignments", "__init__"})


class BuilderError(ValueError):
    """Raised when a builder write is invalid or targets a bad path."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _package_dir(pipeline: str) -> Path:
    from ovid2.web.services import pipelines_meta_svc

    path = pipelines_meta_svc._pipeline_path(pipeline)
    if path is None:
        raise BuilderError(f"Pipeline '{pipeline}' is not installed.")
    return path


def _check_name(name: str) -> None:
    if not _NAME_RE.match(name or "") or name in _RESERVED:
        raise BuilderError(
            f"Invalid name '{name}'. Use lower-case letters, digits and '_' "
            "(starting with a letter); 'assignments' is reserved."
        )


def _parse_yaml(text: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise BuilderError(f"YAML parse error: {exc}") from None


def _atomic_write(
    target: Path, text: str, *, actor: str, action_prefix: str, label: str
) -> dict[str, str]:
    """Write ``text`` atomically and audit; return a WriteResult-shaped dict."""
    if not text.endswith("\n"):
        text += "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.parent.chmod(0o755)
    before = target.read_text(encoding="utf-8") if target.is_file() else ""
    action = "updated" if target.is_file() else "created"
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o644)
    tmp.replace(target)
    web_db.ensure_schema()
    web_db.audit(
        actor=actor,
        action=f"{action_prefix}.{action}",
        target=label,
        before_hash=_hash(before) if before else None,
        after_hash=_hash(text),
        detail_json=json.dumps({"path": str(target)}),
    )
    logger.info("builder %s %s at %s", action, label, target)
    return {"name": label, "path": str(target), "action": action}


def _delete(target: Path, *, actor: str, action: str, label: str) -> dict[str, str]:
    if not target.is_file():
        raise BuilderError(f"'{label}' not found.")
    before = target.read_text(encoding="utf-8")
    target.unlink()
    web_db.ensure_schema()
    web_db.audit(actor=actor, action=action, target=label, before_hash=_hash(before))
    return {"name": label, "path": str(target), "action": "deleted"}


def _doc(path: Path, *, scope: str, pipeline: str | None = None) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        parsed = None
    steps = parsed.get("steps") if isinstance(parsed, dict) else None
    return {
        "name": path.stem,
        "scope": scope,
        "pipeline": pipeline,
        "path": str(path),
        "raw_text": text,
        "parsed": parsed if isinstance(parsed, dict) else None,
        "description": parsed.get("description", "") if isinstance(parsed, dict) else "",
        "steps": len(steps) if isinstance(steps, list) else 0,
        "ops": [s.get("op") for s in steps or [] if isinstance(s, dict)],
    }


# ---------------------------------------------------------------------------
# Transform validation (deeper than transforms_svc: params + expressions)
# ---------------------------------------------------------------------------


def _step_errors(steps: Any, where: str) -> list[str]:
    from jsonschema import Draft202012Validator

    from ovid2.config.sql_expr_guard import check_expression
    from ovid2.config.transforms_catalogue import TRANSFORM_CATALOGUE

    by_name = {str(e["name"]): e for e in TRANSFORM_CATALOGUE}
    errors: list[str] = []
    if not isinstance(steps, list):
        return errors
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        op = step.get("op")
        entry = by_name.get(str(op))
        if entry is None:
            errors.append(f"{where}/{index}/op: unknown transform '{op}'")
            continue
        params = {k: v for k, v in step.items() if k != "op"}
        validator = Draft202012Validator(entry["params_schema"])
        for err in sorted(validator.iter_errors(params), key=lambda e: list(e.path)):
            loc = "/".join(str(p) for p in err.path)
            errors.append(f"{where}/{index}{'/' + loc if loc else ''} ({op}): {err.message}")
        if op in ("derive_column", "filter_expr") and isinstance(params.get("expr"), str):
            result = check_expression(params["expr"])
            if not result.ok:
                errors.append(f"{where}/{index}/expr ({op}): " + "; ".join(result.errors))
    return errors


def validate_transform(raw: Any) -> dict[str, Any]:
    """Validate a transform chain document (schema, ops, params, expressions).

    Returns:
        ``{"ok": bool, "errors": [str]}``.
    """
    from ovid2.web.services import transforms_svc

    if not isinstance(raw, dict):
        return {"ok": False, "errors": ["<root>: a transform spec must be a mapping"]}
    base = transforms_svc.validate_transform_spec(raw)
    errors = [e for e in base["errors"] if "unknown transform" not in e]
    errors.extend(_step_errors(raw.get("steps"), "steps"))
    return {"ok": not errors, "errors": errors}


def _validated_transform_text(raw_text: str, name: str) -> str:
    parsed = _parse_yaml(raw_text)
    report = validate_transform(parsed)
    if not report["ok"]:
        raise BuilderError("; ".join(report["errors"]))
    declared = parsed.get("name")
    if declared and declared != name:
        raise BuilderError(f"Spec 'name: {declared}' does not match the file name '{name}'.")
    return raw_text


# ---------------------------------------------------------------------------
# Transform library (user, XDG)
# ---------------------------------------------------------------------------


def _library_dir() -> Path:
    from ovid2.config.transform_assignments import user_library_dir

    return user_library_dir()


def _shipped_dir() -> Path:
    import ovid2

    return Path(ovid2.__file__).resolve().parent / "resources" / "transforms"


def list_library_transforms() -> list[dict[str, Any]]:
    """Return the user library and the shipped library (read-only) chains."""
    out: list[dict[str, Any]] = []
    for scope, directory in (("user", _library_dir()), ("shipped", _shipped_dir())):
        if directory.is_dir():
            for path in sorted(directory.glob("*.yaml")):
                doc = _doc(path, scope=scope)
                doc.pop("raw_text", None)
                doc.pop("parsed", None)
                out.append(doc)
    return out


def get_library_transform(name: str) -> dict[str, Any] | None:
    """Return a user (then shipped) library chain, or ``None``."""
    _check_name(name)
    for scope, directory in (("user", _library_dir()), ("shipped", _shipped_dir())):
        path = directory / f"{name}.yaml"
        if path.is_file():
            return _doc(path, scope=scope)
    return None


def save_library_transform(name: str, raw_text: str, *, actor: str = "web") -> dict[str, str]:
    """Create or update a user-library chain (validated)."""
    _check_name(name)
    text = _validated_transform_text(raw_text, name)
    return _atomic_write(
        _library_dir() / f"{name}.yaml",
        text,
        actor=actor,
        action_prefix="transform_library",
        label=name,
    )


def delete_library_transform(name: str, *, actor: str = "web") -> dict[str, str]:
    """Delete a user-library chain (shipped chains are never touched)."""
    _check_name(name)
    return _delete(
        _library_dir() / f"{name}.yaml",
        actor=actor,
        action="transform_library.deleted",
        label=name,
    )


# ---------------------------------------------------------------------------
# Pipeline transforms + assignments
# ---------------------------------------------------------------------------


def _transforms_dir(pipeline: str) -> Path:
    return _package_dir(pipeline) / "transforms"


def list_pipeline_transforms(pipeline: str) -> list[dict[str, Any]]:
    """Return the package-private chains (``<pkg>/transforms/*.yaml``)."""
    directory = _transforms_dir(pipeline)
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("*.yaml")):
        if path.stem in _RESERVED:
            continue
        doc = _doc(path, scope="pipeline", pipeline=pipeline)
        doc.pop("raw_text", None)
        doc.pop("parsed", None)
        out.append(doc)
    return out


def get_pipeline_transform(pipeline: str, name: str) -> dict[str, Any] | None:
    """Return one package-private chain, or ``None``."""
    _check_name(name)
    path = _transforms_dir(pipeline) / f"{name}.yaml"
    return _doc(path, scope="pipeline", pipeline=pipeline) if path.is_file() else None


def save_pipeline_transform(
    pipeline: str, name: str, raw_text: str, *, actor: str = "web"
) -> dict[str, str]:
    """Create or update a package-private chain (validated)."""
    _check_name(name)
    text = _validated_transform_text(raw_text, name)
    return _atomic_write(
        _transforms_dir(pipeline) / f"{name}.yaml",
        text,
        actor=actor,
        action_prefix="pipeline_transform",
        label=f"{pipeline}/{name}",
    )


def delete_pipeline_transform(pipeline: str, name: str, *, actor: str = "web") -> dict[str, str]:
    """Delete a package-private chain."""
    _check_name(name)
    return _delete(
        _transforms_dir(pipeline) / f"{name}.yaml",
        actor=actor,
        action="pipeline_transform.deleted",
        label=f"{pipeline}/{name}",
    )


# ---------------------------------------------------------------------------
# UDFs (navigator): apply a declarative chain to a field / table / every table
# ---------------------------------------------------------------------------

#: Where an applied UDF's steps are stored.
UDF_SAVE_TO: tuple[str, ...] = ("inline", "pipeline", "library")
#: Which tables the assignment targets.
UDF_SCOPES: tuple[str, ...] = ("table", "all_tables")


def check_expression_for(
    pipeline: str | None, table: str | None, expression: str
) -> dict[str, Any]:
    """Check a Spark-SQL expression for the UDF dialog.

    Returns ``{ok, errors, columns, functions, unknown_columns}``;
    ``unknown_columns`` lists referenced names that are not columns of the
    table (a warning — the column may be created by an earlier step).
    """
    from ovid2.config.sql_expr_guard import check_expression

    result = check_expression(expression)
    unknown: list[str] = []
    if pipeline and table and result.ok:
        from ovid2.cdm.registry import resolve_cdm_for_pipeline

        try:
            columns = {
                c.upper() for c in resolve_cdm_for_pipeline(pipeline).load_table_columns(table)
            }
        except (ValueError, KeyError, OSError):
            columns = set()
        if columns:
            unknown = [c for c in result.columns if c.upper() not in columns]
    return {
        "ok": result.ok,
        "errors": list(result.errors),
        "columns": list(result.columns),
        "functions": list(result.functions),
        "unknown_columns": unknown,
    }


def allowed_functions() -> list[str]:
    """Return the expression allow-list (sorted) for autocomplete."""
    from ovid2.config.sql_expr_guard import ALLOWED_FUNCTIONS

    return sorted(ALLOWED_FUNCTIONS)


def apply_udf(
    pipeline: str,
    table: str,
    *,
    scope: str,
    save_to: str,
    chain: str | None = None,
    name: str | None = None,
    steps: list[dict[str, Any]] | None = None,
    description: str = "",
    actor: str = "web",
) -> dict[str, Any]:
    """Apply a declarative UDF to a pipeline in one validated write path.

    Exactly one of ``chain`` (an existing library / private chain) or
    ``steps`` (new steps) is given. With ``steps``:

    * ``save_to='inline'`` — the steps go straight into
      ``assignments.yaml`` as an anonymous entry;
    * ``save_to='pipeline'`` / ``'library'`` — they are first saved as a
      named chain (``<pkg>/transforms/<name>.yaml`` or the user library),
      then assigned by name.

    ``scope='table'`` assigns to ``table``; ``'all_tables'`` to ``*``.
    Legacy-parity ports are refused (generated; see ``docs/parity.md``).

    Raises:
        BuilderError: Any validation failure (nothing is written then).
    """
    from ovid2.tools.pipeline_identity import read_settings_constants

    if scope not in UDF_SCOPES:
        raise BuilderError(f"scope must be one of {', '.join(UDF_SCOPES)}")
    if save_to not in UDF_SAVE_TO:
        raise BuilderError(f"save_to must be one of {', '.join(UDF_SAVE_TO)}")
    if (chain is None) == (steps is None):
        raise BuilderError("Give exactly one of 'chain' or 'steps'.")
    settings = _package_dir(pipeline) / "pipeline_settings.py"
    constants = (
        read_settings_constants(settings.read_text(encoding="utf-8")) if settings.is_file() else {}
    )
    if constants.get("ETL_PROFILE") == "legacy_ovid":
        raise BuilderError(
            "legacy_ovid ports are generated byte-parity ports; transforms cannot be "
            "assigned to them (see docs/parity.md)."
        )
    target = "*" if scope == "all_tables" else table
    tables = _cdm_tables(pipeline)
    if target != "*" and tables and target not in tables:
        raise BuilderError(f"'{target}' is not a table of this pipeline's CDM")

    written: list[dict[str, str]] = []
    entry: dict[str, Any]
    if chain is not None:
        _check_name(chain)
        if chain not in _known_chain_names(pipeline):
            raise BuilderError(f"unknown chain '{chain}'")
        entry = {"spec": chain}
    else:
        assert steps is not None
        if not steps:
            raise BuilderError("A UDF needs at least one step.")
        errors = _step_errors(steps, "steps")
        if errors:
            raise BuilderError("; ".join(errors))
        if save_to == "inline":
            entry = {"steps": steps}
        else:
            if not name:
                raise BuilderError("A saved UDF needs a name.")
            _check_name(name)
            doc: dict[str, Any] = {"v": 1}
            if description:
                doc["description"] = description
            doc["steps"] = steps
            text = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
            if save_to == "library":
                existing = get_library_transform(name)
                if existing is not None and existing["scope"] == "shipped":
                    raise BuilderError(f"'{name}' is a shipped chain; pick another name.")
                written.append(save_library_transform(name, text, actor=actor))
            else:
                written.append(save_pipeline_transform(pipeline, name, text, actor=actor))
            entry = {"spec": name}

    current = get_assignments(pipeline)["parsed"] or {"v": 1, "tables": {}}
    current.setdefault("v", 1)
    table_map = current.get("tables") or {}
    current["tables"] = table_map
    entries = table_map.get(target) or []
    table_map[target] = entries
    if not (
        "spec" in entry
        and any(isinstance(e, dict) and e.get("spec") == entry["spec"] for e in entries)
    ):
        entries.append(entry)
    text = yaml.safe_dump(current, sort_keys=False, default_flow_style=False)
    written.append(save_assignments(pipeline, text, actor=actor))
    return {"pipeline": pipeline, "table": target, "entry": entry, "written": written}


def chain_usage(name: str) -> list[dict[str, Any]]:
    """Return where a named chain is assigned: ``[{pipeline, table, fields}]``.

    ``fields`` are the CDM columns the chain's steps write (from the column
    roles), so the UI can show *where used* down to the field.
    """
    from ovid2.config.column_roles import resolve_roles
    from ovid2.config.transform_assignments import AssignmentsError, load_assignments
    from ovid2.config.transforms_catalogue import TRANSFORM_COLUMN_ROLES
    from ovid2.web.services.pipelines_meta_svc import discover_pipelines

    _check_name(name)
    out: list[dict[str, Any]] = []
    for pipeline in discover_pipelines():
        try:
            assigned = load_assignments(pipeline)
        except (AssignmentsError, yaml.YAMLError):
            continue
        hits = [t for t, entries in assigned.items() if any(a.spec == name for a in entries)]
        if not hits:
            continue
        chain = get_pipeline_transform(pipeline, name) or get_library_transform(name) or {}
        steps = (chain.get("parsed") or {}).get("steps") or []
        fields: list[str] = []
        for step in steps:
            if isinstance(step, dict):
                params = {k: v for k, v in step.items() if k != "op"}
                fields.extend(
                    resolve_roles(TRANSFORM_COLUMN_ROLES.get(str(step.get("op"))), params).writes
                )
        for table in hits:
            out.append(
                {"pipeline": pipeline, "table": table, "fields": list(dict.fromkeys(fields))}
            )
    return out


def _known_chain_names(pipeline: str) -> set[str]:
    names = {d["name"] for d in list_library_transforms()}
    names.update(d["name"] for d in list_pipeline_transforms(pipeline))
    return names


def validate_assignments(pipeline: str, raw: Any) -> dict[str, Any]:
    """Validate an assignments document against schema, CDM tables and chains."""
    from ovid2.config.transform_assignments import AssignmentsError, parse_assignments

    try:
        parsed = parse_assignments(raw, origin="assignments")
    except AssignmentsError as exc:
        return {"ok": False, "errors": [str(exc)]}
    errors: list[str] = []
    tables = _cdm_tables(pipeline)
    known = _known_chain_names(pipeline)
    for table, entries in parsed.items():
        if table != "*" and tables and table not in tables:
            errors.append(f"tables/{table}: not a table of this pipeline's CDM")
        for i, entry in enumerate(entries):
            if entry.spec and entry.spec not in known:
                errors.append(f"tables/{table}/{i}: unknown chain '{entry.spec}'")
            if entry.steps is not None:
                errors.extend(_step_errors(list(entry.steps), f"tables/{table}/{i}/steps"))
    return {"ok": not errors, "errors": errors}


def _cdm_tables(pipeline: str) -> list[str]:
    from ovid2.cdm.registry import resolve_cdm_for_pipeline

    try:
        return list(resolve_cdm_for_pipeline(pipeline).tables())
    except ValueError:
        return []


def get_assignments(pipeline: str) -> dict[str, Any]:
    """Return the raw + parsed assignments (empty template when absent)."""
    path = _transforms_dir(pipeline) / "assignments.yaml"
    text = path.read_text(encoding="utf-8") if path.is_file() else "v: 1\ntables: {}\n"
    parsed = yaml.safe_load(text) or {}
    return {
        "pipeline": pipeline,
        "path": str(path),
        "exists": path.is_file(),
        "raw_text": text,
        "parsed": parsed,
    }


def save_assignments(pipeline: str, raw_text: str, *, actor: str = "web") -> dict[str, str]:
    """Write ``<pkg>/transforms/assignments.yaml`` (validated)."""
    parsed = _parse_yaml(raw_text)
    report = validate_assignments(pipeline, parsed)
    if not report["ok"]:
        raise BuilderError("; ".join(report["errors"]))
    return _atomic_write(
        _transforms_dir(pipeline) / "assignments.yaml",
        raw_text,
        actor=actor,
        action_prefix="transform_assignments",
        label=pipeline,
    )


def assign_chain(
    pipeline: str, chain: str, tables: list[str], *, actor: str = "web"
) -> dict[str, str]:
    """Append ``{spec: chain}`` to each table's assignments (idempotent)."""
    _check_name(chain)
    current = get_assignments(pipeline)["parsed"] or {"v": 1, "tables": {}}
    current.setdefault("v", 1)
    table_map = current.setdefault("tables", {}) or {}
    current["tables"] = table_map
    for table in tables:
        entries = table_map.setdefault(table, []) or []
        table_map[table] = entries
        if not any(isinstance(e, dict) and e.get("spec") == chain for e in entries):
            entries.append({"spec": chain})
    text = yaml.safe_dump(current, sort_keys=False, default_flow_style=False)
    return save_assignments(pipeline, text, actor=actor)


# ---------------------------------------------------------------------------
# Fixers
# ---------------------------------------------------------------------------


def get_fixers(pipeline: str) -> dict[str, Any]:
    """Return the fixer chains as YAML text (converted from ``.py`` when needed)."""
    from ovid2.config.fixer_configs import (
        FixerConfigsError,
        dump_fixer_yaml,
        fixer_yaml_path,
        load_fixer_configs,
    )

    path = fixer_yaml_path(pipeline)
    if path is not None:
        text = path.read_text(encoding="utf-8")
        try:
            load_fixer_configs(pipeline)
            error = ""
        except FixerConfigsError as exc:
            error = str(exc)
        return {
            "pipeline": pipeline,
            "source": "yaml",
            "path": str(path),
            "raw_text": text,
            "error": error,
        }
    chains, multi, source = load_fixer_configs(pipeline)
    text = (
        dump_fixer_yaml(chains, multi)
        if source
        else "v: 1\nfixer_chains: {}\nmulti_table_fixes: []\n"
    )
    return {
        "pipeline": pipeline,
        "source": source or "none",
        "path": str(_package_dir(pipeline) / "fixer_configs.yaml"),
        "raw_text": text,
        "error": "",
    }


def validate_fixers(raw: Any) -> dict[str, Any]:
    """Validate a fixer_configs document (schema + fixer registry)."""
    from ovid2.config.fixer_configs import validate_fixer_document

    errors = validate_fixer_document(raw)
    return {"ok": not errors, "errors": errors}


def save_fixers(pipeline: str, raw_text: str, *, actor: str = "web") -> dict[str, str]:
    """Write ``<pkg>/fixer_configs.yaml`` (validated; wins over ``.py``)."""
    from ovid2.tools.pipeline_identity import read_settings_constants

    settings = _package_dir(pipeline) / "pipeline_settings.py"
    constants = (
        read_settings_constants(settings.read_text(encoding="utf-8")) if settings.is_file() else {}
    )
    if constants.get("ETL_PROFILE") == "legacy_ovid":
        raise BuilderError(
            "legacy_ovid ports run their vendored legacy fixer scripts; native fix "
            "chains are not allowed (see docs/parity.md)."
        )
    parsed = _parse_yaml(raw_text)
    report = validate_fixers(parsed)
    if not report["ok"]:
        raise BuilderError("; ".join(report["errors"]))
    return _atomic_write(
        _package_dir(pipeline) / "fixer_configs.yaml",
        raw_text,
        actor=actor,
        action_prefix="fixers",
        label=pipeline,
    )


# ---------------------------------------------------------------------------
# Live validation (editor lint)
# ---------------------------------------------------------------------------


def _split_path(message: str) -> dict[str, str]:
    """Split ``"steps/2/expr (op): msg"`` into ``{path, message}``."""
    head, sep, _rest = message.partition(": ")
    if (sep and "/" in head.split(" ")[0]) or head.startswith(("<root>", "tables", "fixer_chains")):
        return {"path": head.split(" ")[0], "message": message}
    return {"path": "", "message": message}


def validate_document(pipeline: str | None, doc_type: str, raw_text: str) -> dict[str, Any]:
    """Validate one builder document without saving it.

    Args:
        pipeline: Owning package (``None`` / unknown is fine for transforms).
        doc_type: ``transform`` | ``assignments`` | ``fixers`` | ``formatter``
            | ``mapper``.
        raw_text: YAML text.

    Returns:
        ``{"ok": bool, "errors": [{"path": str, "message": str}]}``.
    """
    try:
        parsed = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        return {"ok": False, "errors": [{"path": "", "message": f"YAML parse error: {exc}"}]}
    if doc_type == "transform":
        report = validate_transform(parsed)
    elif doc_type == "assignments":
        report = validate_assignments(pipeline or "", parsed)
    elif doc_type == "fixers":
        report = validate_fixers(parsed)
    elif doc_type in ("formatter", "mapper"):
        report = _validate_by_schema(
            "formatter_spec.v1.json" if doc_type == "formatter" else "mapper_spec.v1.json", parsed
        )
        if doc_type == "mapper" and report["ok"] and isinstance(parsed, dict):
            report = _validate_mapper_ops(parsed)
    else:
        report = {"ok": False, "errors": [f"unknown document type '{doc_type}'"]}
    return {"ok": report["ok"], "errors": [_split_path(str(e)) for e in report["errors"]]}


def _validate_by_schema(schema_name: str, parsed: Any) -> dict[str, Any]:
    from importlib import resources

    from jsonschema import Draft202012Validator

    ref = resources.files("ovid2.resources.schemas") / schema_name
    schema = json.loads(ref.read_text(encoding="utf-8"))
    errors = [
        f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
        for e in sorted(
            Draft202012Validator(schema).iter_errors(parsed), key=lambda e: list(e.path)
        )
    ]
    return {"ok": not errors, "errors": errors}


def _validate_mapper_ops(parsed: dict[str, Any]) -> dict[str, Any]:
    from jsonschema import Draft202012Validator

    from ovid2.config.mapper_ops_catalogue import MAPPER_OP_CATALOGUE

    errors: list[str] = []
    for index, op in enumerate(parsed.get("ops") or []):
        if not isinstance(op, dict):
            continue
        name = str(op.get("op", ""))
        entry = MAPPER_OP_CATALOGUE.get(name)
        if entry is None:
            errors.append(f"ops/{index}/op: unknown mapper op '{name}'")
            continue
        params = {k: v for k, v in op.items() if k != "op"}
        for err in Draft202012Validator(entry["params_schema"]).iter_errors(params):
            loc = "/".join(str(p) for p in err.path)
            errors.append(f"ops/{index}{'/' + loc if loc else ''} ({name}): {err.message}")
        if name == "apply_transforms" and isinstance(params.get("steps"), list):
            errors.extend(_step_errors(params["steps"], f"ops/{index}/steps"))
    return {"ok": not errors, "errors": errors}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def get_settings(pipeline: str) -> dict[str, Any]:
    """Return every module-level constant with whether the UI may edit it."""
    import ast

    from ovid2.tools.pipeline_identity import IDENTITY_FIELDS, read_settings_constants

    path = _package_dir(pipeline) / "pipeline_settings.py"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    literal = read_settings_constants(text)
    meta = {f.key: f for f in IDENTITY_FIELDS}
    rows: list[dict[str, Any]] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = ast.Module(body=[], type_ignores=[])
    seen: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id in seen:
            continue
        name = target.id
        if not (name.isupper() or name in ("cdm", "mappers_type")):
            continue
        seen.add(name)
        editable = isinstance(node.value, ast.Constant) and isinstance(
            node.value.value, (str, int, float, bool)
        )
        alias = node.value.id if isinstance(node.value, ast.Name) else ""
        field = meta.get(name)
        rows.append(
            {
                "key": name,
                "value": literal.get(name),
                "type": type(literal.get(name)).__name__ if name in literal else "expression",
                "editable": editable,
                "alias_of": alias,
                "label": field.label if field else "",
                "category": field.category if field else "other",
                "must_match": field.must_match if field else "",
                "choices": list(field.choices) if field else [],
            }
        )
    return {"pipeline": pipeline, "path": str(path), "constants": rows}


def save_settings(pipeline: str, values: dict[str, Any], *, actor: str = "web") -> dict[str, str]:
    """Rewrite literal constants in ``pipeline_settings.py`` (formatting kept).

    Only existing literal constants may change and only to a value of the
    same JSON-scalar type; ``cdm`` must be a registered CDM.
    """
    from ovid2.cdm.registry import registered_cdms
    from ovid2.tools.pipeline_identity import IDENTITY_FIELDS, rewrite_settings

    current = {row["key"]: row for row in get_settings(pipeline)["constants"]}
    choices = {f.key: f.choices for f in IDENTITY_FIELDS if f.choices}
    clean: dict[str, Any] = {}
    for key, value in values.items():
        row = current.get(key)
        if row is None:
            raise BuilderError(f"'{key}' is not a constant of pipeline_settings.py.")
        if not row["editable"]:
            raise BuilderError(f"'{key}' is computed in Python; edit it on disk.")
        if row["value"] == value:
            continue
        old_type = type(row["value"])
        if old_type is bool:
            if not isinstance(value, bool):
                raise BuilderError(f"'{key}' must be true/false.")
        elif old_type in (int, float):
            try:
                value = old_type(value)
            except (TypeError, ValueError):
                raise BuilderError(f"'{key}' must be a number.") from None
        else:
            value = str(value)
        if key in choices and value not in choices[key]:
            raise BuilderError(f"'{key}' must be one of {', '.join(choices[key])}.")
        if key == "cdm" and value not in registered_cdms():
            raise BuilderError(f"CDM '{value}' is not registered.")
        clean[key] = value
    path = _package_dir(pipeline) / "pipeline_settings.py"
    text = path.read_text(encoding="utf-8")
    if not clean:
        return {"name": pipeline, "path": str(path), "action": "unchanged"}
    new_text = rewrite_settings(text, clean)
    compile(new_text, str(path), "exec")
    return _atomic_write(path, new_text, actor=actor, action_prefix="settings", label=pipeline)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("This module is imported by the ovid2 web routes; it is not a script.")
