"""Report-source adapter: ``ovid2 analysis`` aggregates.

Input: the records of ``analysis-<instance_id>.jsonl`` written by the
``monthly_counts`` / ``distinct_values`` transformers
(:mod:`ovid2.reports.artifacts`). Record shapes::

    {"type": "monthly",  "table": t, "column": c, "rows": [{"month": "2024-01", "records": n}, ...]}
    {"type": "distinct", "table": t, "column": c, "total_rows": n, "n_distinct": k,
     "top": [{"value": v, "records": n, "percentage": p}, ...], "stage": "formatted|raw"}

Datasets: ``monthly`` (long), ``monthly_totals`` (per table, first date
column), ``columns`` (distinct-value profile), ``top_values`` (value
frequencies — values are ``row_values``), ``tables`` (row counts).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ovid2.reports.sources import column, dataset, make_source

__all__ = ["build"]


def build(
    records: list[Mapping[str, Any]],
    *,
    instance_id: str,
    pipeline: str | None = None,
    batch_id: str | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    """Return a report source built from analysis artifact records."""
    monthly: list[dict[str, Any]] = []
    first_date_col: dict[str, str] = {}
    profile: list[dict[str, Any]] = []
    top_values: list[dict[str, Any]] = []
    table_rows: dict[str, int] = {}
    for rec in records:
        table = str(rec.get("table") or "")
        col = str(rec.get("column") or "")
        if rec.get("type") == "monthly":
            first_date_col.setdefault(table, col)
            for r in rec.get("rows") or []:
                month = r.get("month")
                if not month:
                    continue
                monthly.append(
                    {
                        "table": table,
                        "column": col,
                        "month": f"{month}-01" if len(str(month)) == 7 else month,
                        "records": int(r.get("records") or 0),
                    }
                )
        elif rec.get("type") == "distinct":
            total = int(rec.get("total_rows") or 0)
            table_rows[table] = max(table_rows.get(table, 0), total)
            profile.append(
                {
                    "table": table,
                    "column": col,
                    "stage": rec.get("stage") or "formatted",
                    "distinct_values": int(rec.get("n_distinct") or 0),
                    "total_rows": total,
                    "null_rows": int(rec.get("null_rows") or 0),
                    "null_pct": round(100.0 * int(rec.get("null_rows") or 0) / total, 2)
                    if total
                    else 0.0,
                }
            )
            for rank, t in enumerate(rec.get("top") or [], start=1):
                value = t.get("value")
                top_values.append(
                    {
                        "table": table,
                        "column": col,
                        "rank": rank,
                        "value": "(null)" if value is None else str(value)[:200],
                        "records": int(t.get("records") or 0),
                        "percentage": float(t.get("percentage") or 0.0),
                    }
                )
    totals = [
        {"table": r["table"], "month": r["month"], "records": r["records"]}
        for r in monthly
        if first_date_col.get(r["table"]) == r["column"]
    ]
    datasets = {
        "monthly": dataset(
            "Monthly record counts per date column",
            [
                column("table", "nominal", "Table"),
                column("column", "nominal", "Date column"),
                column("month", "temporal", "Month"),
                column("records", "quantitative", "Records", is_count=True),
            ],
            monthly,
        ),
        "monthly_totals": dataset(
            "Monthly volume per table (primary date column)",
            [
                column("table", "nominal", "Table"),
                column("month", "temporal", "Month"),
                column("records", "quantitative", "Records", is_count=True),
            ],
            totals,
        ),
        "columns": dataset(
            "Column profile",
            [
                column("table", "nominal", "Table"),
                column("column", "nominal", "Column"),
                column("stage", "nominal", "Stage"),
                column("distinct_values", "quantitative", "Distinct values"),
                column("total_rows", "quantitative", "Rows", is_count=True),
                column("null_rows", "quantitative", "Null rows", is_count=True),
                column("null_pct", "quantitative", "Null %"),
            ],
            profile,
        ),
        "top_values": dataset(
            "Most frequent values",
            [
                column("table", "nominal", "Table"),
                column("column", "nominal", "Column"),
                column("rank", "ordinal", "Rank"),
                column("value", "nominal", "Value", sensitivity="row_values"),
                column("records", "quantitative", "Records", is_count=True),
                column("percentage", "quantitative", "% of rows"),
            ],
            top_values,
        ),
        "tables": dataset(
            "Rows per table",
            [
                column("table", "nominal", "Table"),
                column("rows", "quantitative", "Rows", is_count=True),
            ],
            [{"table": t, "rows": n} for t, n in sorted(table_rows.items())],
        ),
    }
    n_tables = len({r.get("table") for r in records})
    overview = (
        f"Analysis profile of **{n_tables}** table(s) for pipeline `{pipeline or '—'}`: "
        f"{len(first_date_col)} with monthly volumes, {len(profile)} profiled column(s)."
    )
    return make_source(
        kind="analysis",
        key=key or instance_id,
        title=f"Analysis — {pipeline or 'run'} {batch_id or ''}".strip(),
        datasets=datasets,
        texts={"overview": overview},
        instance_id=instance_id,
        pipeline=pipeline,
        batch_id=batch_id,
        templates=["analysis.volume_profile"],
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("library module")
