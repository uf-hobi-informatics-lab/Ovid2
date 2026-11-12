"""Compose a layout + source + report theme into a resolved render model.

The render model is plain JSON. The Studio preview (browser), the
self-contained HTML export and the PDF / DOCX / XLSX / image exporters
all consume it, so every surface shows the same thing:

- hidden sections and blocks are dropped (never exported);
- block filters, top-N and **small-cell suppression** are applied before
  anything is charted, tabulated or aggregated;
- charts become complete, themed Vega-Lite specs (inline data);
- KPI values are computed in Python (no raw rows are embedded);
- markdown is rendered with raw HTML disabled (safe to inject).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from markdown_it import MarkdownIt

from ovid2.reports.catalogue import ReportTheme, get_chart, get_theme
from ovid2.reports.charts import build_spec
from ovid2.reports.data import (
    SUPPRESSED,
    aggregate,
    apply_filters,
    apply_top_n,
    count_columns,
    suppress_small_cells,
)

__all__ = ["format_number", "markdown_html", "render_model"]

_MD = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": True}).enable(
    "table"
)


def markdown_html(text: str) -> str:
    """Render markdown to HTML with raw HTML disabled (XSS-safe)."""
    html: str = _MD.render(text or "")
    return html


def format_number(value: Any, fmt: str = "number") -> str:
    """Format a KPI / table number consistently across every renderer."""
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "yes" if value else "no"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isnan(v) or math.isinf(v):
        return "—"
    if fmt == "raw":
        return str(value)
    if fmt == "percent":
        return f"{v:.1%}" if abs(v) <= 1.0 else f"{v:.1f}%"
    if fmt == "integer":
        return f"{round(v):,}"
    if fmt == "compact":
        for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
            if abs(v) >= div:
                return f"{v / div:.1f}{suffix}"
        return f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"
    if v == int(v) and abs(v) < 1e15:
        return f"{int(v):,}"
    return f"{v:,.4g}" if abs(v) < 1e-3 or abs(v) >= 1e6 else f"{v:,.2f}"


def _block_rows(
    block: Mapping[str, Any], dataset: Mapping[str, Any], threshold: int
) -> tuple[list[dict[str, Any]], int]:
    rows = apply_filters(dataset.get("rows") or [], block.get("filters"))
    rows, hits = suppress_small_cells(rows, count_columns(dataset.get("columns") or []), threshold)
    rows = apply_top_n(rows, block.get("top_n"))
    return rows, hits


def _kpi(
    params: Mapping[str, Any], rows: list[dict[str, Any]], cols: Mapping[str, Any]
) -> dict[str, Any]:
    raw = aggregate(rows, params["value"], params.get("agg", "sum"))
    fmt = params.get("format", "number")
    label = params.get("label") or str(
        (cols.get(params["value"]) or {}).get("title") or params["value"]
    )
    status = None
    threshold = params.get("threshold")
    if isinstance(threshold, (int, float)) and isinstance(raw, (int, float)):
        above = raw >= threshold
        status = (
            "good"
            if (above if params.get("good_when", "above") == "above" else not above)
            else "bad"
        )
    return {
        "value": raw,
        "display": format_number(raw, fmt) + (params.get("suffix") or ""),
        "label": label,
        "status": status,
        "threshold": threshold,
    }


def _table(
    params: Mapping[str, Any], rows: list[dict[str, Any]], columns: list[Mapping[str, Any]]
) -> dict[str, Any]:
    names = [str(c["name"]) for c in columns]
    chosen = [c for c in (params.get("columns") or names) if c in names][:20]
    meta = {str(c["name"]): c for c in columns}
    max_rows = int(params.get("max_rows", 200))
    fmt = params.get("number_format", "number")
    out_rows = []
    for row in rows[:max_rows]:
        cells = []
        for c in chosen:
            v = row.get(c)
            if v is None and meta[c].get("is_count") and c in row:
                cells.append(SUPPRESSED)
            elif meta[c].get("type") == "quantitative":
                cells.append(format_number(v, fmt))
            else:
                cells.append("" if v is None else str(v))
        out_rows.append(cells)
    return {
        "columns": [{"name": c, "title": str(meta[c].get("title") or c)} for c in chosen],
        "rows": out_rows,
        "status_column": params.get("status_column")
        if params.get("status_column") in chosen
        else None,
        "zebra": bool(params.get("zebra", True)),
        "total_rows": len(rows),
        "truncated": len(rows) > max_rows,
    }


def _render_block(
    block: Mapping[str, Any],
    source: Mapping[str, Any],
    theme: ReportTheme,
    threshold: int,
    width: int | str,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": block["id"],
        "type": block["type"],
        "title": block.get("title") or "",
        "caption": block.get("caption") or "",
        "width": block.get("width", "full"),
    }
    btype = block["type"]
    if btype in ("text", "callout"):
        text = block.get("text") or (source.get("texts") or {}).get(block.get("text_ref") or "", "")
        base.update(
            renderer=btype, html=markdown_html(text), markdown=text, tone=block.get("tone", "info")
        )
        return base
    kind = get_chart(str(block.get("chart")))
    dataset = (source.get("datasets") or {}).get(str(block.get("dataset")))
    if kind is None or not isinstance(dataset, Mapping):
        base.update(
            renderer="error",
            error=f"chart {block.get('chart')!r} / dataset {block.get('dataset')!r} unavailable",
        )
        return base
    columns = list(dataset.get("columns") or [])
    rows, hits = _block_rows(block, dataset, threshold)
    params = dict(block.get("params") or {})
    base.update(
        chart=kind.name, dataset=block.get("dataset"), suppressed_cells=hits, row_count=len(rows)
    )
    base["data"] = {"columns": [dict(c) for c in columns], "rows": rows}
    cols = {str(c["name"]): c for c in columns}
    try:
        if kind.renderer == "kpi":
            base.update(renderer="kpi", kpi=_kpi(params, rows, cols))
        elif kind.renderer == "table":
            base.update(renderer="table", table=_table(params, rows, columns))
        else:
            spec = build_spec(kind.name, params, rows, columns, theme, width=width)
            base.update(renderer="vega", spec=spec)
    except (KeyError, TypeError, ValueError) as exc:
        base.update(renderer="error", error=f"could not build {kind.name}: {exc}")
    return base


def render_model(
    layout: Mapping[str, Any],
    source: Mapping[str, Any],
    *,
    width: int | str = "container",
    meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the resolved, theme-applied render model for ``layout``.

    Args:
        layout: A validated layout document.
        source: The report source it references.
        width: ``"container"`` (browser) or a pixel width (exporters).
        meta: Optional report metadata (``report_id``, ``version``,
            ``owner``, ``updated_at``) stamped into the provenance block.
    """
    theme = get_theme(layout.get("theme"))
    options = dict(layout.get("options") or {})
    threshold = int(options.get("small_cell_threshold") or 0)
    sections: list[dict[str, Any]] = []
    suppressed = 0
    for section in layout.get("sections") or []:
        if section.get("visible", True) is False:
            continue
        blocks = []
        for block in section.get("blocks") or []:
            if block.get("visible", True) is False:
                continue
            rendered = _render_block(block, source, theme, threshold, width)
            suppressed += int(rendered.get("suppressed_cells") or 0)
            blocks.append(rendered)
        sections.append(
            {
                "id": section["id"],
                "title": section["title"],
                "description_html": markdown_html(section.get("description") or ""),
                "description": section.get("description") or "",
                "page_break_before": bool(section.get("page_break_before")),
                "blocks": blocks,
            }
        )
    provenance = {
        "source_id": source.get("source_id"),
        "source_kind": source.get("kind"),
        "source_title": source.get("title"),
        "instance_id": source.get("instance_id"),
        "pipeline": source.get("pipeline"),
        "batch_id": source.get("batch_id"),
        "cdm": source.get("cdm"),
        "source_generated_at": source.get("generated_at"),
        "small_cell_threshold": threshold,
        "suppressed_cells": suppressed,
    }
    provenance.update({k: v for k, v in (meta or {}).items() if v is not None})
    return {
        "v": 1,
        "title": layout.get("title") or "Report",
        "subtitle": layout.get("subtitle") or "",
        "description_html": markdown_html(layout.get("description") or ""),
        "description": layout.get("description") or "",
        "theme": theme.as_dict(),
        "page": {"size": "letter", "orientation": "portrait", **(layout.get("page") or {})},
        "options": {"show_provenance": True, "show_toc": False, **options},
        "provenance": provenance,
        "sections": sections,
    }


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("library module")
