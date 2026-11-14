"""Executor-only report renderers (PDF, DOCX, PPTX, XLSX, HTML, SVG/PNG, MD, JSON).

Never imported by the web / MCP tiers (``test_tier_import_closure.py``):
they pull in reportlab, python-docx, python-pptx, xlsxwriter and vl-convert. Entry
point: :func:`export_report`, driven by ``ovid2 tools report-export``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ovid2.reports.formats import EXPORT_FORMATS

__all__ = ["EXPORT_FORMATS", "ExportError", "ExportResult", "export_report", "safe_stem"]


class ExportError(RuntimeError):
    """Raised when an export cannot be produced."""


@dataclass(frozen=True)
class ExportResult:
    """A finished export file."""

    path: Path
    filename: str
    media_type: str
    size: int


_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_stem(title: str) -> str:
    """Return a filesystem-safe filename stem for ``title``."""
    return _STEM_RE.sub("_", title).strip("._")[:80] or "report"


def export_report(
    bundle: Mapping[str, Any],
    fmt: str,
    out_dir: Path,
    *,
    block_id: str | None = None,
) -> ExportResult:
    """Render ``bundle`` (``{layout, source, meta}``) to ``fmt`` in ``out_dir``.

    Args:
        bundle: ``layout`` (validated), ``source`` (report source) and
            optional ``meta`` (report id / version / owner).
        fmt: One of :data:`EXPORT_FORMATS`.
        out_dir: Existing directory to write the file into.
        block_id: For ``svg`` / ``png``: export only this chart block.
    """
    if fmt not in EXPORT_FORMATS:
        raise ExportError(f"unknown export format {fmt!r}")
    from ovid2.reports.compose import render_model

    layout = bundle["layout"]
    source = bundle["source"]
    meta = dict(bundle.get("meta") or {})
    ext, media, _label = EXPORT_FORMATS[fmt]
    stem = safe_stem(str(layout.get("title") or "report"))
    if fmt == "json":
        from ovid2.reports.render.simple import write_json

        path = write_json(layout, source, meta, out_dir / f"{stem}.json")
        return _result(path, media)
    model = render_model(layout, source, width=_PAGE_CHART_WIDTH.get(fmt, 640), meta=meta)
    writers: dict[str, Callable[..., Path]] = {}
    if fmt == "pdf":
        from ovid2.reports.render.pdf import write_pdf

        writers[fmt] = write_pdf
    elif fmt == "docx":
        from ovid2.reports.render.docx import write_docx

        writers[fmt] = write_docx
    elif fmt == "pptx":
        from ovid2.reports.render.pptx import write_pptx

        writers[fmt] = write_pptx
    elif fmt == "xlsx":
        from ovid2.reports.render.xlsx import write_xlsx

        writers[fmt] = write_xlsx
    elif fmt == "html":
        from ovid2.reports.render.html import write_html

        writers[fmt] = write_html
    elif fmt == "md":
        from ovid2.reports.render.simple import write_markdown_zip

        writers[fmt] = write_markdown_zip
    if fmt in ("svg", "png"):
        from ovid2.reports.render.simple import write_images

        path, media = write_images(model, out_dir, stem, fmt, block_id=block_id)
        return _result(path, media)
    path = writers[fmt](model, out_dir / f"{stem}.{ext}")
    return _result(path, media)


#: Chart pixel width per output (fits the page / document body).
_PAGE_CHART_WIDTH: dict[str, int] = {
    "pdf": 620,
    "docx": 620,
    "pptx": 1100,
    "xlsx": 640,
    "html": 760,
    "md": 680,
    "svg": 720,
    "png": 720,
}


def _result(path: Path, media: str) -> ExportResult:
    path.chmod(0o600)
    return ExportResult(path=path, filename=path.name, media_type=media, size=path.stat().st_size)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("library module")
