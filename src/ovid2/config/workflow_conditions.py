"""Workflow branch conditions — field catalogue, validator and evaluator.

A workflow edge may carry a ``when`` condition that decides whether the
downstream step runs, given what the upstream step produced
(adr-workflows.md, "Branching"). A condition is a small boolean tree::

    {"all": [cond, ...]}          # AND
    {"any": [cond, ...]}          # OR
    {"not": cond}                 # NOT
    {"field": "health.blockers", "op": "eq", "value": 0}   # leaf

Leaves test one *fact* of the finished upstream step. Facts come from a
closed catalogue (:data:`FIELDS`), never from row values: run status and
error, the event-log timeline (table statuses, warnings, fixes), the
health report summary, the tables present at the output, analysis row
counts, the engine's rows-written metric and — opt-in, per step — the
numbers returned by *data probes* (aggregate counts the executor computes
with a guarded row filter; see :data:`PROBE_AGGS`).

This module is tier-neutral pure Python (no engine / web imports): the web
tier validates and evaluates with it, the canvas receives
:func:`catalogue` as JSON, and the executor-side probe tool shares
:func:`validate_probe`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "EDGE_ON",
    "FIELDS",
    "JOIN_MODES",
    "MAX_DEPTH",
    "MAX_LEAVES",
    "MAX_PROBES",
    "OPS",
    "OPS_BY_TYPE",
    "PROBE_AGGS",
    "FieldSpec",
    "catalogue",
    "evaluate",
    "explain",
    "field_groups",
    "lookup_field",
    "resolve",
    "summarise",
    "validate_condition",
    "validate_probe",
]

#: When an edge is followed, relative to the upstream step's outcome.
#:
#: - ``success``   — the upstream step finished successfully (default);
#: - ``failure``   — the upstream step failed (a recovery / notify path);
#: - ``always``    — either way;
#: - ``otherwise`` — the upstream step succeeded and *none* of its other
#:   conditional ``success`` edges matched (the "else" branch).
EDGE_ON: tuple[str, ...] = ("success", "failure", "always", "otherwise")

#: How a step with several incoming edges decides to run.
#:
#: - ``all`` — every incoming edge must be taken; one branch not taken
#:   (condition false, or its upstream step skipped) skips the step;
#: - ``any`` — wait for every upstream step to settle, then run when at
#:   least one incoming edge was taken.
JOIN_MODES: tuple[str, ...] = ("all", "any")

#: Value types a field may have.
_TYPES: tuple[str, ...] = ("str", "num", "bool", "list")

#: Every operator and the number of operands it takes (``0`` = none,
#: ``1`` = scalar, ``2`` = a two-item list, ``-1`` = a non-empty list).
OPS: dict[str, int] = {
    "eq": 1,
    "ne": 1,
    "gt": 1,
    "gte": 1,
    "lt": 1,
    "lte": 1,
    "between": 2,
    "in": -1,
    "not_in": -1,
    "contains": 1,
    "not_contains": 1,
    "starts_with": 1,
    "ends_with": 1,
    "matches": 1,
    "contains_any": -1,
    "contains_all": -1,
    "is_empty": 0,
    "is_not_empty": 0,
    "exists": 0,
    "not_exists": 0,
    "is_true": 0,
    "is_false": 0,
}

#: Operators that make sense per field type (the canvas offers only these).
OPS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "num": (
        "eq",
        "ne",
        "gt",
        "gte",
        "lt",
        "lte",
        "between",
        "in",
        "not_in",
        "exists",
        "not_exists",
    ),
    "str": (
        "eq",
        "ne",
        "contains",
        "not_contains",
        "starts_with",
        "ends_with",
        "matches",
        "in",
        "not_in",
        "is_empty",
        "is_not_empty",
        "exists",
        "not_exists",
    ),
    "bool": ("is_true", "is_false", "eq", "ne", "exists", "not_exists"),
    "list": (
        "contains",
        "not_contains",
        "contains_any",
        "contains_all",
        "starts_with",
        "ends_with",
        "matches",
        "is_empty",
        "is_not_empty",
    ),
}

#: Human labels for the operators (canvas select + summaries).
OP_LABELS: dict[str, str] = {
    "eq": "=",
    "ne": "≠",
    "gt": ">",
    "gte": "≥",
    "lt": "<",
    "lte": "≤",
    "between": "between",
    "in": "is one of",
    "not_in": "is not one of",
    "contains": "contains",
    "not_contains": "does not contain",
    "starts_with": "starts with",
    "ends_with": "ends with",
    "matches": "matches regex",
    "contains_any": "contains any of",
    "contains_all": "contains all of",
    "is_empty": "is empty",
    "is_not_empty": "is not empty",
    "exists": "is known",
    "not_exists": "is unknown",
    "is_true": "is true",
    "is_false": "is false",
}

#: Aggregates a data probe may compute — numbers only, never row values.
PROBE_AGGS: tuple[str, ...] = ("count", "count_distinct", "null_count", "non_null_count")

MAX_DEPTH = 6
MAX_LEAVES = 40
MAX_PROBES = 10
_MAX_REGEX = 200
_NUMERIC_VALUE_OPS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte", "between", "in", "not_in"})
_MAX_VALUE_LEN = 500
_MAX_LIST = 100
_PARAM_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_PROBE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_TABLE_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")
_COLUMN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


@dataclass(frozen=True)
class FieldSpec:
    """One condition field.

    Attributes:
        name: Field name, or a prefix ending in ``.`` for parameterised
            fields (``table.<T>.status`` is ``prefix="table."``,
            ``suffix=".status"``).
        type: ``str`` / ``num`` / ``bool`` / ``list``.
        group: Fact group the runner must gather to resolve it
            (``run`` / ``timeline`` / ``health`` / ``output`` / ``analysis`` /
            ``metrics`` / ``query``).
        label: Short UI label.
        help: One-line explanation (rendered under a "?" tip).
        param: Name of the ``<param>`` for parameterised fields.
        suffix: Text after the parameter.
        choices: Known values (the canvas renders a select).
    """

    name: str
    type: str
    group: str
    label: str
    help: str
    param: str = ""
    suffix: str = ""
    choices: tuple[str, ...] = ()


_STEP_OUTCOMES = ("succeeded", "failed")
_TABLE_STATUSES = ("pending", "running", "completed", "failed")
_CHECK_STATUSES = ("passed", "exception", "error", "skipped", "not_applicable")
_SEVERITIES = ("blocker", "warning", "info")

#: The closed field catalogue.
FIELDS: tuple[FieldSpec, ...] = (
    # --- run -------------------------------------------------------------
    FieldSpec(
        "status", "str", "run", "Step outcome", "succeeded or failed.", choices=_STEP_OUTCOMES
    ),
    FieldSpec(
        "error", "str", "run", "Error message", "The failed step's error line (empty on success)."
    ),
    FieldSpec("duration_s", "num", "run", "Duration (s)", "Wall-clock seconds the step's job ran."),
    FieldSpec("pipeline", "str", "run", "Pipeline", "Saved pipeline config the step ran."),
    FieldSpec("input_address", "str", "run", "Input address", "Address the step read from."),
    FieldSpec("output_address", "str", "run", "Output address", "Address the step wrote to."),
    FieldSpec(
        "batch_id", "str", "run", "Batch", "The run's batch label (the watched folder path)."
    ),
    FieldSpec(
        "root_input",
        "str",
        "run",
        "Run input",
        "Address the run was started on (watch batch / manual).",
    ),
    FieldSpec(
        "trigger",
        "str",
        "run",
        "Trigger",
        "What started the run.",
        choices=("manual", "cron", "interval", "watch"),
    ),
    # --- timeline (event log) --------------------------------------------
    FieldSpec(
        "tables_completed", "list", "timeline", "Tables completed", "Tables the step finished."
    ),
    FieldSpec("tables_failed", "list", "timeline", "Tables failed", "Tables the step failed on."),
    FieldSpec(
        "tables_completed_count",
        "num",
        "timeline",
        "# tables completed",
        "How many tables finished.",
    ),
    FieldSpec(
        "tables_failed_count", "num", "timeline", "# tables failed", "How many tables failed."
    ),
    FieldSpec("warnings", "num", "timeline", "Warnings", "Warning events in the step's log."),
    FieldSpec("errors", "num", "timeline", "Errors", "Error events in the step's log."),
    FieldSpec("fixes_applied", "num", "timeline", "Fixes applied", "Fixer applications recorded."),
    FieldSpec(
        "table.",
        "str",
        "timeline",
        "Table status",
        "Status of one table in the step's log.",
        param="table",
        suffix=".status",
        choices=_TABLE_STATUSES,
    ),
    # --- health report ---------------------------------------------------
    FieldSpec(
        "health.available",
        "bool",
        "health",
        "Health report present",
        "The step wrote a health report.",
    ),
    FieldSpec("health.checks_total", "num", "health", "Checks run", "Health checks run."),
    FieldSpec(
        "health.checks_passed", "num", "health", "Checks passed", "Health checks that passed."
    ),
    FieldSpec(
        "health.checks_failed",
        "num",
        "health",
        "Checks failed",
        "Checks with an exception or error.",
    ),
    FieldSpec(
        "health.pass_rate",
        "num",
        "health",
        "Pass rate (%)",
        "Share of checks run that passed, in percent.",
    ),
    FieldSpec("health.blockers", "num", "health", "Blockers", "Failed checks of severity blocker."),
    FieldSpec(
        "health.failed_by_severity.",
        "num",
        "health",
        "Failed by severity",
        "Failed checks of one severity.",
        param="severity",
        choices=_SEVERITIES,
    ),
    FieldSpec(
        "health.check.",
        "str",
        "health",
        "Check status",
        "Status of one health check.",
        param="check",
        suffix=".status",
        choices=_CHECK_STATUSES,
    ),
    FieldSpec(
        "health.failed_checks",
        "list",
        "health",
        "Failed check ids",
        "Ids of the checks that failed.",
    ),
    # --- output (brokered table listing) ---------------------------------
    FieldSpec(
        "output.tables", "list", "output", "Output tables", "Tables present at the output address."
    ),
    FieldSpec(
        "output.table_count",
        "num",
        "output",
        "# output tables",
        "How many tables the output holds.",
    ),
    # --- analysis / metrics ----------------------------------------------
    FieldSpec(
        "rows.",
        "num",
        "analysis",
        "Row count",
        "Rows in one table (from the step's analysis profile, when it ran one).",
        param="table",
    ),
    FieldSpec(
        "rows_written_total",
        "num",
        "metrics",
        "Rows written",
        "Rows written, as reported by the engine.",
    ),
    # --- data probes -----------------------------------------------------
    FieldSpec(
        "query.",
        "num",
        "query",
        "Data probe",
        "Number returned by a data probe declared on the upstream step.",
        param="probe",
    ),
)

_BY_NAME: dict[str, FieldSpec] = {f.name: f for f in FIELDS if not f.param}
_PARAMETERISED: tuple[FieldSpec, ...] = tuple(f for f in FIELDS if f.param)


def lookup_field(name: str) -> tuple[FieldSpec, str] | None:
    """Return ``(spec, param)`` for a field name, or ``None`` when unknown."""
    if not isinstance(name, str):
        return None
    if name in _BY_NAME:
        return _BY_NAME[name], ""
    for spec in _PARAMETERISED:
        if not name.startswith(spec.name):
            continue
        rest = name[len(spec.name) :]
        if spec.suffix:
            if not rest.endswith(spec.suffix):
                continue
            rest = rest[: -len(spec.suffix)]
        if rest and _PARAM_RE.match(rest):
            return spec, rest
    return None


def catalogue() -> dict[str, Any]:
    """Return the JSON catalogue the canvas renders its condition builder from."""
    return {
        "on": list(EDGE_ON),
        "join": list(JOIN_MODES),
        "ops": {k: {"arity": v, "label": OP_LABELS[k]} for k, v in OPS.items()},
        "ops_by_type": {k: list(v) for k, v in OPS_BY_TYPE.items()},
        "probe_aggs": list(PROBE_AGGS),
        "fields": [
            {
                "name": f.name,
                "type": f.type,
                "group": f.group,
                "label": f.label,
                "help": f.help,
                "param": f.param,
                "suffix": f.suffix,
                "choices": list(f.choices),
            }
            for f in FIELDS
        ],
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _check_value(spec: FieldSpec, op: str, value: Any, where: str) -> list[str]:
    arity = OPS[op]
    errors: list[str] = []
    if arity == 0:
        return errors
    if arity == 2:
        if not isinstance(value, list) or len(value) != 2:
            return [f"{where}: '{op}' needs two values [low, high]."]
        items = value
    elif arity == -1:
        if not isinstance(value, list) or not value:
            return [f"{where}: '{op}' needs a non-empty list of values."]
        if len(value) > _MAX_LIST:
            return [f"{where}: at most {_MAX_LIST} values."]
        items = value
    else:
        if isinstance(value, (list, dict)) or value is None:
            return [f"{where}: '{op}' needs a single value."]
        items = [value]
    for item in items:
        if isinstance(item, (list, dict)) or item is None:
            errors.append(f"{where}: values must be text, numbers or true/false.")
            break
        if isinstance(item, str) and len(item) > _MAX_VALUE_LEN:
            errors.append(f"{where}: a value is longer than {_MAX_VALUE_LEN} characters.")
            break
        if spec.type == "num" and op in _NUMERIC_VALUE_OPS and _to_num(item) is None:
            errors.append(f"{where}: '{item}' is not a number.")
            break
    if op == "matches":
        pattern = str(value)
        if len(pattern) > _MAX_REGEX:
            errors.append(f"{where}: regex longer than {_MAX_REGEX} characters.")
        else:
            try:
                re.compile(pattern)
            except re.error as exc:
                errors.append(f"{where}: invalid regex ({exc}).")
    if arity == 2 and not errors and spec.type == "num":
        lo, hi = (_to_num(v) for v in value)
        if lo is not None and hi is not None and lo > hi:
            errors.append(f"{where}: 'between' low value is above the high value.")
    return errors


def validate_condition(
    cond: Any, *, probes: frozenset[str] | set[str] | None = None, where: str = "when"
) -> list[str]:
    """Validate a condition tree; return a list of errors (empty = valid).

    Args:
        cond: The ``when`` value.
        probes: Probe names declared on the upstream step; ``query.<name>``
            must name one. ``None`` skips the check.
        where: Location label for messages.
    """
    errors: list[str] = []
    leaves = 0

    def walk(node: Any, path: str, depth: int) -> None:
        nonlocal leaves
        if depth > MAX_DEPTH:
            errors.append(f"{path}: nested deeper than {MAX_DEPTH} levels.")
            return
        if not isinstance(node, dict):
            errors.append(f"{path}: a condition must be an object.")
            return
        keys = set(node)
        if keys & {"all", "any"}:
            if len(keys) != 1:
                errors.append(f"{path}: a group has exactly one key ('all' or 'any').")
                return
            key = next(iter(keys))
            items = node[key]
            if not isinstance(items, list) or not items:
                errors.append(f"{path}.{key}: needs a non-empty list.")
                return
            for i, child in enumerate(items):
                walk(child, f"{path}.{key}[{i}]", depth + 1)
            return
        if "not" in keys:
            if len(keys) != 1:
                errors.append(f"{path}: 'not' must be the only key.")
                return
            walk(node["not"], f"{path}.not", depth + 1)
            return
        leaves += 1
        extra = keys - {"field", "op", "value", "case_sensitive"}
        if extra:
            errors.append(f"{path}: unknown key(s) {sorted(extra)}.")
        found = lookup_field(node.get("field", ""))
        if found is None:
            errors.append(f"{path}: unknown field '{node.get('field')}'.")
            return
        spec, param = found
        op = node.get("op")
        if op not in OPS:
            errors.append(f"{path}: unknown operator '{op}'.")
            return
        if op not in OPS_BY_TYPE[spec.type]:
            errors.append(
                f"{path}: operator '{op}' does not apply to {spec.type} field '{node['field']}'."
            )
            return
        if "case_sensitive" in node and not isinstance(node["case_sensitive"], bool):
            errors.append(f"{path}: case_sensitive must be true or false.")
        if spec.group == "query" and probes is not None and param not in probes:
            errors.append(f"{path}: no data probe named '{param}' on the upstream step.")
        errors.extend(_check_value(spec, op, node.get("value"), path))

    walk(cond, where, 1)
    if leaves > MAX_LEAVES:
        errors.append(f"{where}: at most {MAX_LEAVES} tests per condition.")
    return errors


def validate_probe(probe: Any, *, where: str = "probe") -> list[str]:
    """Validate one data-probe declaration (the ``where`` filter is checked too).

    A probe is ``{"name", "table", "agg", "column"?, "where"?}``; ``where``
    is a row-level Spark-SQL predicate that must pass
    :mod:`ovid2.config.sql_expr_guard`.
    """
    if not isinstance(probe, dict):
        return [f"{where}: must be an object."]
    errors: list[str] = []
    extra = set(probe) - {"name", "table", "agg", "column", "where"}
    if extra:
        errors.append(f"{where}: unknown key(s) {sorted(extra)}.")
    name = probe.get("name")
    if not isinstance(name, str) or not _PROBE_NAME_RE.match(name):
        errors.append(f"{where}: name must be lower-case letters, digits and '_' (max 40).")
    table = probe.get("table")
    if not isinstance(table, str) or not _TABLE_RE.match(table):
        errors.append(f"{where}: table must be a table name.")
    agg = probe.get("agg", "count")
    if agg not in PROBE_AGGS:
        errors.append(f"{where}: agg must be one of {', '.join(PROBE_AGGS)}.")
    column = probe.get("column")
    if agg != "count":
        if not isinstance(column, str) or not _COLUMN_RE.match(column):
            errors.append(f"{where}: '{agg}' needs a column name.")
    elif column not in (None, "") and (not isinstance(column, str) or not _COLUMN_RE.match(column)):
        errors.append(f"{where}: column must be a column name.")
    flt = probe.get("where")
    if flt not in (None, ""):
        from ovid2.config.sql_expr_guard import check_expression

        result = check_expression(str(flt))
        if not result.ok:
            errors.append(f"{where}: filter rejected — " + "; ".join(result.errors))
    return errors


def field_groups(cond: Any) -> set[str]:
    """Return the fact groups a condition needs (``run`` is always cheap)."""
    groups: set[str] = set()

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        for key in ("all", "any"):
            if isinstance(node.get(key), list):
                for child in node[key]:
                    walk(child)
                return
        if "not" in node:
            walk(node["not"])
            return
        found = lookup_field(node.get("field", ""))
        if found is not None:
            groups.add(found[0].group)

    walk(cond)
    return groups


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

_MISSING = object()


def resolve(name: str, facts: dict[str, Any]) -> Any:
    """Return the value of field ``name`` from a facts dict (``_MISSING`` sentinel → ``None``).

    The facts dict is the structured record the runner gathers per step
    (see ``workflow_runner_svc._gather_facts``). Unknown / unavailable
    values resolve to ``None``.
    """
    value = _resolve(name, facts)
    return None if value is _MISSING else value


def _ci_get(mapping: Any, key: str) -> Any:
    if not isinstance(mapping, dict):
        return _MISSING
    if key in mapping:
        return mapping[key]
    low = key.lower()
    for k, v in mapping.items():
        if str(k).lower() == low:
            return v
    return _MISSING


def _resolve(name: str, facts: dict[str, Any]) -> Any:
    found = lookup_field(name)
    if found is None:
        return _MISSING
    spec, param = found
    if spec.name == "table.":
        return _ci_get(facts.get("tables"), param)
    if spec.name == "health.failed_by_severity.":
        health = facts.get("health") or {}
        if not health.get("available"):
            return _MISSING
        count = _ci_get(health.get("failed_by_severity"), param)
        return 0 if count is _MISSING or count is None else count
    if spec.name == "health.check.":
        return _ci_get((facts.get("health") or {}).get("checks"), param)
    if spec.name == "rows.":
        return _ci_get(facts.get("rows"), param)
    if spec.name == "query.":
        return _ci_get(facts.get("query"), param)
    if name.startswith("health."):
        health = facts.get("health") or {}
        key = name.removeprefix("health.")
        if key == "available":
            return bool(health.get("available"))
        if not health.get("available"):
            return _MISSING
        return health.get(key, _MISSING)
    if name.startswith("output."):
        out = facts.get("output") or {}
        if out.get("tables") is None:
            return _MISSING
        return out["tables"] if name == "output.tables" else len(out["tables"])
    return facts.get(name, _MISSING)


def _to_num(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _norm(value: Any, cs: bool) -> str:
    text = "" if value is None else str(value)
    if isinstance(value, bool):
        text = "true" if value else "false"
    return text if cs else text.lower()


def _scalar_eq(actual: Any, expected: Any, spec: FieldSpec, cs: bool) -> bool:
    if spec.type == "num":
        a, b = _to_num(actual), _to_num(expected)
        return a is not None and b is not None and a == b
    if spec.type == "bool":
        return _norm(bool(actual), False) == _norm(expected, False)
    return _norm(actual, cs) == _norm(expected, cs)


def _text_op(op: str, text: str, needle: str) -> bool:
    if op == "contains":
        return needle in text
    if op == "starts_with":
        return text.startswith(needle)
    if op == "ends_with":
        return text.endswith(needle)
    return False


def _leaf(node: dict[str, Any], facts: dict[str, Any]) -> bool:
    found = lookup_field(node.get("field", ""))
    if found is None:
        return False
    spec, _param = found
    op = str(node.get("op"))
    value: Any = node.get("value")
    cs = bool(node.get("case_sensitive", False))
    actual = _resolve(str(node["field"]), facts)
    missing = actual is _MISSING or actual is None
    if op == "exists":
        return not missing
    if op == "not_exists":
        return missing
    if op in ("is_empty", "is_not_empty"):
        empty = missing or (isinstance(actual, (list, str)) and len(actual) == 0)
        return empty if op == "is_empty" else not empty
    if missing:
        # Negative tests on an unknown value hold; positive ones do not.
        return op in ("ne", "not_in", "not_contains")
    if op == "is_true":
        return bool(actual) is True
    if op == "is_false":
        return bool(actual) is False
    if spec.type == "list":
        items = [_norm(x, cs) for x in (actual if isinstance(actual, list) else [actual])]
        if op == "contains":
            return _norm(value, cs) in items
        if op == "not_contains":
            return _norm(value, cs) not in items
        if op == "contains_any":
            return any(_norm(v, cs) in items for v in value)
        if op == "contains_all":
            return all(_norm(v, cs) in items for v in value)
        if op in ("starts_with", "ends_with"):
            return any(_text_op(op, x, _norm(value, cs)) for x in items)
        if op == "matches":
            rx = re.compile(str(value), 0 if cs else re.IGNORECASE)
            return any(
                rx.search(str(x)) for x in (actual if isinstance(actual, list) else [actual])
            )
        return False
    if op == "eq":
        return _scalar_eq(actual, value, spec, cs)
    if op == "ne":
        return not _scalar_eq(actual, value, spec, cs)
    if op == "in":
        return any(_scalar_eq(actual, v, spec, cs) for v in value)
    if op == "not_in":
        return not any(_scalar_eq(actual, v, spec, cs) for v in value)
    if op in ("gt", "gte", "lt", "lte", "between"):
        a = _to_num(actual)
        if a is None:
            return False
        if op == "between":
            lo, hi = _to_num(value[0]), _to_num(value[1])
            return lo is not None and hi is not None and lo <= a <= hi
        b = _to_num(value)
        if b is None:
            return False
        return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
    text = _norm(actual, cs)[:4096]
    if op == "not_contains":
        return _norm(value, cs) not in text
    if op == "matches":
        return re.search(str(value), str(actual)[:4096], 0 if cs else re.IGNORECASE) is not None
    return _text_op(op, text, _norm(value, cs))


def evaluate(cond: Any, facts: dict[str, Any]) -> bool:
    """Evaluate a (validated) condition against a facts dict.

    ``None`` / an empty condition is ``True``. Unknown facts make positive
    tests false and negative tests (``ne`` / ``not_in`` / ``not_contains``
    / ``is_empty`` / ``not_exists``) true.
    """
    if cond is None or cond == {}:
        return True
    if not isinstance(cond, dict):
        return False
    if "all" in cond:
        return all(evaluate(c, facts) for c in cond["all"])
    if "any" in cond:
        return any(evaluate(c, facts) for c in cond["any"])
    if "not" in cond:
        return not evaluate(cond["not"], facts)
    return _leaf(cond, facts)


def _fmt_value(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_fmt_value(v) for v in value) + "]"
    if isinstance(value, str):
        return repr(value) if " " in value or not value else value
    return str(value)


def summarise(cond: Any, *, limit: int = 0) -> str:
    """Return a short human-readable rendering of a condition."""

    def text(node: Any) -> str:
        if not isinstance(node, dict):
            return "?"
        if "all" in node:
            return "(" + " AND ".join(text(c) for c in node["all"]) + ")"
        if "any" in node:
            return "(" + " OR ".join(text(c) for c in node["any"]) + ")"
        if "not" in node:
            return "NOT " + text(node["not"])
        op = str(node.get("op"))
        out = f"{node.get('field')} {OP_LABELS.get(op, op)}"
        if OPS.get(op, 0) != 0:
            out += " " + _fmt_value(node.get("value"))
        return out

    out = text(cond) if cond else ""
    if isinstance(cond, dict) and ("all" in cond or "any" in cond):
        out = out[1:-1]
    if limit and len(out) > limit:
        out = out[: limit - 1] + "…"
    return out


def explain(cond: Any, facts: dict[str, Any]) -> str:
    """Return ``"<summary> → true/false"`` plus the actual values of its fields."""
    seen: list[str] = []

    def fields(node: Any) -> None:
        if not isinstance(node, dict):
            return
        for key in ("all", "any"):
            if isinstance(node.get(key), list):
                for c in node[key]:
                    fields(c)
                return
        if "not" in node:
            fields(node["not"])
            return
        name = str(node.get("field"))
        if name not in seen:
            seen.append(name)

    fields(cond)
    actual = ", ".join(f"{n} = {_fmt_value(resolve(n, facts))}" for n in seen[:6])
    return f"{summarise(cond, limit=160)} → {str(evaluate(cond, facts)).lower()} ({actual})"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("library module")
