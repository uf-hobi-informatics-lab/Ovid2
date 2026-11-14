"""Column roles — which columns a transform / mapper op / fixer touches.

Tier-neutral (pure Python): the web lineage service
(:mod:`ovid2.web.services.pipeline_lineage_svc`) reads it to draw the
per-field chain of functions without importing pipeline code.

A *role* is a small dict declared next to each catalogue entry
(:data:`ovid2.config.transforms_catalogue.TRANSFORM_COLUMN_ROLES`,
:data:`ovid2.config.mapper_ops_catalogue.MAPPER_OP_COLUMN_ROLES`,
:data:`ovid2.config.fixers_catalogue.FIXER_COLUMN_ROLES`)::

    {
        "scope": "field" | "rows" | "shape" | "table",
        "inplace": [token, ...],   # columns read and rewritten in place
        "reads":   [token, ...],   # columns only read
        "writes":  [token, ...],   # columns written (new or replaced)
        "exprs":   [token, ...],   # params holding Spark-SQL expressions
        "tables":  [token, ...],   # params naming *reference tables*
        "by_table": {table: COLUMN},  # column chosen by the fixed table
        "filters_rows": bool,      # also drops rows (field-scoped fixers)
    }

``scope`` says how the step is drawn: ``field`` steps sit on the
columns they touch; ``rows`` (filters, dedup, sampling), ``shape``
(select / rename / pivot) and ``table`` steps are bands across the
whole table.

Token grammar (resolved against the step's params):

* ``name`` — the param's value: a string or a list of strings.
* ``name:keys`` / ``name:values`` — keys / values of a mapping param.
* ``name[].col`` — the ``col`` of every object in a list param.
* ``name:csv`` — a comma-separated string, split.
* ``a|b`` — the first alternative that resolves to something.
* An UPPERCASE token (fixers only) is a literal column name.

Fixer params are the positional ``src1`` / ``src2`` slots; the sentinel
``NotUsed`` resolves to nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ROLE_KEYS",
    "SCOPES",
    "ResolvedRoles",
    "resolve_roles",
    "token_params",
]

#: Valid ``scope`` values.
SCOPES: tuple[str, ...] = ("field", "rows", "shape", "table")

#: Keys a role dict may carry.
ROLE_KEYS: frozenset[str] = frozenset(
    {"scope", "inplace", "reads", "writes", "exprs", "tables", "by_table", "filters_rows"}
)

_LITERAL = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SENTINELS = frozenset({"", "notused", "not_used", "none"})


@dataclass
class ResolvedRoles:
    """Columns one step touches, resolved against its params.

    Attributes:
        scope: Drawing scope (see :data:`SCOPES`).
        reads: Columns read but not written.
        writes: Columns written (including in-place rewrites).
        exprs: Expression strings (callers extract their columns).
        tables: Reference-table names the step reads.
        filters_rows: ``True`` when the step also removes rows.
    """

    scope: str = "field"
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    exprs: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    filters_rows: bool = False


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [] if value.strip().lower() in _SENTINELS else [value.strip()]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


def _resolve_one(token: str, params: dict[str, Any], *, literals: bool) -> list[str]:
    if literals and _LITERAL.match(token):
        return [token]
    if "[]." in token:
        name, _, sub = token.partition("[].")
        items = params.get(name)
        if not isinstance(items, list):
            return []
        return [s for item in items if isinstance(item, dict) for s in _strings(item.get(sub))]
    name, _, mod = token.partition(":")
    value = params.get(name)
    if mod == "keys":
        return [str(k) for k in value] if isinstance(value, dict) else []
    if mod == "values":
        return _strings(list(value.values())) if isinstance(value, dict) else []
    if mod == "csv":
        return [p.strip() for s in _strings(value) for p in s.split(",") if p.strip()]
    return _strings(value)


def _resolve(token: str, params: dict[str, Any], *, literals: bool) -> list[str]:
    for alternative in token.split("|"):
        found = _resolve_one(alternative.strip(), params, literals=literals)
        if found:
            return found
    return []


def resolve_roles(
    role: dict[str, Any] | None,
    params: dict[str, Any],
    *,
    table: str = "",
    literals: bool = False,
) -> ResolvedRoles:
    """Resolve a role against one step's params.

    Args:
        role: The role dict (``None`` → an empty table-scoped step).
        params: The step's params (``op`` / ``fix`` keys are ignored).
        table: The table being processed (for ``by_table``).
        literals: Treat UPPERCASE tokens as literal column names (fixers).

    Returns:
        The :class:`ResolvedRoles`; never raises.
    """
    if not role:
        return ResolvedRoles(scope="table")

    def many(key: str) -> list[str]:
        out: list[str] = []
        for token in role.get(key) or ():
            out.extend(_resolve(str(token), params, literals=literals))
        return list(dict.fromkeys(out))

    inplace = many("inplace")
    writes = list(dict.fromkeys([*inplace, *many("writes")]))
    reads = [c for c in many("reads") if c not in writes]
    by_table = role.get("by_table") or {}
    if table and table in by_table:
        column = str(by_table[table])
        if column not in reads and column not in writes:
            reads.append(column)
    return ResolvedRoles(
        scope=str(role.get("scope") or "field"),
        reads=reads,
        writes=writes,
        exprs=many("exprs"),
        tables=many("tables"),
        filters_rows=bool(role.get("filters_rows")),
    )


def token_params(role: dict[str, Any]) -> set[str]:
    """Return every *param name* a role's tokens reference (for contract tests)."""
    names: set[str] = set()
    for key in ("inplace", "reads", "writes", "exprs", "tables"):
        for token in role.get(key) or ():
            for alternative in str(token).split("|"):
                alternative = alternative.strip()
                if _LITERAL.match(alternative):
                    continue
                base = alternative.split("[].", 1)[0].split(":", 1)[0]
                names.add(base)
    return names


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.config.column_roles is a library module.")
