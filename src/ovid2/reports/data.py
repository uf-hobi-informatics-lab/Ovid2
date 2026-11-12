"""Row-level helpers for report datasets (pure Python, small data only).

Report datasets are *already aggregated* (check metrics, monthly counts,
value frequencies, stage timings) and capped at
:data:`MAX_DATASET_ROWS`, so plain Python lists are the right tool — this
module never touches Spark.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

__all__ = [
    "MAX_DATASET_ROWS",
    "SUPPRESSED",
    "aggregate",
    "apply_filters",
    "apply_top_n",
    "count_columns",
    "suppress_small_cells",
]

#: Hard cap on rows stored per dataset in a report source.
MAX_DATASET_ROWS = 5000

#: Marker written in place of a suppressed small count (tables / exports).
SUPPRESSED = "<suppressed>"


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)) and not (isinstance(value, float) and math.isnan(value)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _match(row: Mapping[str, Any], flt: Mapping[str, Any]) -> bool:
    field = str(flt.get("field"))
    op = flt.get("op")
    want = flt.get("value")
    have = row.get(field)
    if op == "is_null":
        return have is None or have == ""
    if op == "not_null":
        return not (have is None or have == "")
    if op == "eq":
        return bool(have == want or (str(have) == str(want) and want is not None))
    if op == "ne":
        return not (have == want or str(have) == str(want))
    if op in ("in", "not_in"):
        values = want if isinstance(want, list) else [want]
        hit = have in values or str(have) in {str(v) for v in values}
        return hit if op == "in" else not hit
    if op == "contains":
        return want is not None and str(want).lower() in str(have or "").lower()
    a, b = _num(have), _num(want)
    if a is None or b is None:
        return False
    return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}.get(str(op), False)


def apply_filters(
    rows: Iterable[Mapping[str, Any]], filters: list[Mapping[str, Any]] | None
) -> list[dict[str, Any]]:
    """Return the rows matching **all** ``filters`` (copies)."""
    out = [dict(r) for r in rows]
    for flt in filters or []:
        out = [r for r in out if _match(r, flt)]
    return out


def apply_top_n(rows: list[dict[str, Any]], top: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Return the top ``n`` rows by ``by`` (descending unless asked)."""
    if not top:
        return rows
    by = str(top.get("by"))
    n = int(top.get("n", 10))
    reverse = top.get("order", "descending") == "descending"

    def key(r: Mapping[str, Any]) -> float:
        v = _num(r.get(by))
        return v if v is not None else (-math.inf if reverse else math.inf)

    return sorted(rows, key=key, reverse=reverse)[:n]


def count_columns(columns: Iterable[Mapping[str, Any]]) -> set[str]:
    """Return the names of columns flagged ``is_count``."""
    return {str(c["name"]) for c in columns if c.get("is_count")}


def suppress_small_cells(
    rows: list[dict[str, Any]], count_cols: set[str], threshold: int
) -> tuple[list[dict[str, Any]], int]:
    """Null out counts in ``(0, threshold)``; return ``(rows, cells_suppressed)``.

    Applied *after* filtering and before any aggregation or rendering, so
    no chart, table, KPI or export can reveal a small cell.
    """
    if threshold <= 0 or not count_cols:
        return rows, 0
    hits = 0
    out: list[dict[str, Any]] = []
    for row in rows:
        new = dict(row)
        for col in count_cols:
            v = _num(new.get(col))
            if v is not None and 0 < v < threshold:
                new[col] = None
                hits += 1
        out.append(new)
    return out, hits


def aggregate(rows: Sequence[Mapping[str, Any]], field: str, how: str) -> float | str | None:
    """Reduce one column to a single value (for KPI / gauge / progress)."""
    values = [r.get(field) for r in rows]
    if how == "count":
        return float(sum(1 for v in values if v is not None and v != ""))
    if how == "distinct":
        return float(len({str(v) for v in values if v is not None}))
    if how in ("first", "last"):
        present = [v for v in values if v is not None]
        if not present:
            return None
        v = present[0] if how == "first" else present[-1]
        n = _num(v)
        return n if n is not None else str(v)
    nums = [n for n in (_num(v) for v in values) if n is not None]
    if not nums:
        return None
    if how == "sum":
        return float(sum(nums))
    if how == "mean":
        return float(statistics.fmean(nums))
    if how == "median":
        return float(statistics.median(nums))
    if how == "min":
        return float(min(nums))
    if how == "max":
        return float(max(nums))
    return float(sum(nums))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("library module")
