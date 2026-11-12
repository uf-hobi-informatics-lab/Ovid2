"""Image (SVG / PNG), Markdown and JSON-layout exports."""

from __future__ import annotations

import json
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ovid2.reports.render._common import chart_png, chart_svg

__all__ = ["write_images", "write_json", "write_markdown_zip"]


def _charts(model: Mapping[str, Any], block_id: str | None) -> list[Mapping[str, Any]]:
    out = [b for s in model["sections"] for b in s["blocks"] if b["renderer"] == "vega"]
    if block_id:
        out = [b for b in out if b["id"] == block_id]
    return out


def _image(b: Mapping[str, Any], fmt: str) -> bytes:
    spec = dict(b["spec"])
    if b.get("title") and "title" not in spec:
        spec["title"] = b["title"]
    return chart_svg(spec).encode("utf-8") if fmt == "svg" else chart_png(spec, scale=2.0)


def write_images(
    model: Mapping[str, Any], out_dir: Path, stem: str, fmt: str, *, block_id: str | None = None
) -> tuple[Path, str]:
    """Write one image (single chart) or a zip of every visible chart."""
    from ovid2.reports.render import ExportError

    charts = _charts(model, block_id)
    if not charts:
        raise ExportError(
            "no visible chart" + (f" with id {block_id!r}" if block_id else "") + " to export"
        )
    if len(charts) == 1:
        path = out_dir / f"{stem}_{charts[0]['id']}.{fmt}"
        path.write_bytes(_image(charts[0], fmt))
        return path, "image/svg+xml" if fmt == "svg" else "image/png"
    path = out_dir / f"{stem}_charts_{fmt}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, b in enumerate(charts, start=1):
            zf.writestr(f"{n:02d}_{b['id']}.{fmt}", _image(b, fmt))
    return path, "application/zip"


def write_markdown_zip(model: Mapping[str, Any], path: Path) -> Path:
    """Write ``report.md`` + ``figures/*.svg`` + ``data/*.csv`` in a zip."""
    import csv
    import io

    lines = [f"# {model['title']}", ""]
    if model.get("subtitle"):
        lines += [f"_{model['subtitle']}_", ""]
    if model.get("description"):
        lines += [model["description"], ""]
    files: dict[str, bytes] = {}
    for s in model["sections"]:
        lines += [f"## {s['title']}", ""]
        if s.get("description"):
            lines += [s["description"], ""]
        for b in s["blocks"]:
            r = b["renderer"]
            if b.get("title") and r != "kpi":
                lines += [f"### {b['title']}", ""]
            if r in ("text", "callout"):
                body = b.get("markdown") or ""
                lines += (
                    [("> " + ln) for ln in body.splitlines()] if r == "callout" else [body]
                ) + [""]
            elif r == "vega":
                name = f"figures/{b['id']}.svg"
                files[name] = chart_svg(b["spec"]).encode("utf-8")
                lines += [f"![{b.get('title') or b['id']}]({name})", ""]
            elif r == "kpi":
                k = b["kpi"]
                lines += [f"**{k['label']}:** {k['display']}", ""]
            elif r == "table":
                t = b["table"]
                if t["rows"]:
                    lines.append("| " + " | ".join(c["title"] for c in t["columns"]) + " |")
                    lines.append("|" + "---|" * len(t["columns"]))
                    for row in t["rows"]:
                        lines.append(
                            "| "
                            + " | ".join(str(v).replace("|", "\\|").replace("\n", " ") for v in row)
                            + " |"
                        )
                    lines.append("")
            if b.get("caption"):
                lines += [f"_{b['caption']}_", ""]
            data = b.get("data")
            if data and data.get("rows"):
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow([c["name"] for c in data["columns"]])
                for row in data["rows"]:
                    writer.writerow(
                        [
                            "" if row.get(c["name"]) is None else row.get(c["name"])
                            for c in data["columns"]
                        ]
                    )
                files[f"data/{b['id']}.csv"] = buf.getvalue().encode("utf-8")
    if model["options"].get("show_provenance", True):
        lines += ["---", "", "**Provenance**", ""]
        lines += [
            f"- {k.replace('_', ' ')}: `{v}`"
            for k, v in model["provenance"].items()
            if v not in (None, "")
        ]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("report.md", "\n".join(lines) + "\n")
        for name, data_bytes in files.items():
            zf.writestr(name, data_bytes)
    return path


def write_json(
    layout: Mapping[str, Any], source: Mapping[str, Any], meta: Mapping[str, Any], path: Path
) -> Path:
    """Write the re-importable layout definition (no data rows)."""
    doc = {
        "format": "ovid2.report_layout",
        "v": 1,
        "exported_at": datetime.now(UTC).isoformat(),
        "source": {
            "source_id": source.get("source_id"),
            "kind": source.get("kind"),
            "title": source.get("title"),
        },
        "report": {k: v for k, v in meta.items() if k in ("report_id", "report_version", "owner")},
        "layout": layout,
    }
    path.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    return path


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("library module")
