"""Report Studio export format catalogue (tier-neutral, no renderer imports)."""

from __future__ import annotations

__all__ = ["EXPORT_FORMATS"]

#: format → (extension, media type, label)
EXPORT_FORMATS: dict[str, tuple[str, str, str]] = {
    "pdf": ("pdf", "application/pdf", "PDF document"),
    "html": ("html", "text/html", "Self-contained HTML (interactive, offline)"),
    "docx": (
        "docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "Word document",
    ),
    "pptx": (
        "pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "PowerPoint presentation (16:9 slides)",
    ),
    "xlsx": (
        "xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "Excel data workbook",
    ),
    "svg": ("svg", "image/svg+xml", "Charts as SVG (zip, or one file for a single chart)"),
    "png": ("png", "image/png", "Charts as PNG (zip, or one file for a single chart)"),
    "md": ("zip", "application/zip", "Markdown + figures (zip)"),
    "json": ("json", "application/json", "Layout definition (re-importable JSON)"),
}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("library module")
