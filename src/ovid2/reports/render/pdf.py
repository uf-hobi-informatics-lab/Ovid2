"""PDF export (reportlab; charts embedded as vector graphics via svglib)."""

from __future__ import annotations

import io
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from ovid2.reports.render._common import MdBlock, chart_svg, md_blocks, status_color

__all__ = ["write_pdf"]

_FONT_MAP = {
    "serif": ("Times-Roman", "Times-Bold", "Times-Italic"),
    "sans": ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique"),
    "mono": ("Courier", "Courier-Bold", "Courier-Oblique"),
}


def _family(css: str) -> tuple[str, str, str]:
    low = css.lower()
    if any(k in low for k in ("georgia", "times", "palatino", "serif")) and "sans-serif" not in low:
        return _FONT_MAP["serif"]
    if any(k in low for k in ("courier", "mono")):
        return _FONT_MAP["mono"]
    return _FONT_MAP["sans"]


def _runs_markup(block: MdBlock) -> str:
    parts = []
    for r in block.runs:
        t = escape(r.text).replace("\n", "<br/>")
        if r.code:
            t = f'<font face="Courier">{t}</font>'
        if r.bold:
            t = f"<b>{t}</b>"
        if r.italic:
            t = f"<i>{t}</i>"
        parts.append(t)
    return "".join(parts)


def write_pdf(model: Mapping[str, Any], path: Path) -> Path:
    """Write the render model as a paginated, themed PDF."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4, LEGAL, LETTER, landscape, portrait
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        CondPageBreak,
        KeepTogether,
        LongTable,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        TableStyle,
    )
    from reportlab.platypus import Table as RLTable
    from svglib.svglib import svg2rlg

    theme = model["theme"]
    c = theme["colors"]
    sizes = theme["sizes"]
    body, bold, italic = _family(theme["fonts"].get("body", ""))
    head_font = _family(theme["fonts"].get("heading", ""))[1]
    page = model.get("page") or {}
    size = {"a4": A4, "legal": LEGAL}.get(str(page.get("size")), LETTER)
    size = landscape(size) if page.get("orientation") == "landscape" else portrait(size)
    margin = 0.6 * inch
    frame_w = size[0] - 2 * margin

    def hx(v: str) -> Any:
        return colors.HexColor(v)

    base = int(sizes.get("base", 11))
    st = {
        "title": ParagraphStyle(
            "t",
            fontName=head_font,
            fontSize=sizes.get("title", 20),
            leading=sizes.get("title", 20) * 1.2,
            textColor=hx(c["text"]),
            spaceAfter=4,
        ),
        "subtitle": ParagraphStyle(
            "st",
            fontName=italic,
            fontSize=base + 2,
            leading=(base + 2) * 1.3,
            textColor=hx(c["muted"]),
            spaceAfter=8,
        ),
        "h1": ParagraphStyle(
            "h1",
            fontName=head_font,
            fontSize=sizes.get("heading", 15),
            leading=sizes.get("heading", 15) * 1.3,
            textColor=hx(c["text"]),
            spaceBefore=10,
            spaceAfter=6,
        ),
        "h2": ParagraphStyle(
            "h2",
            fontName=head_font,
            fontSize=base + 2,
            leading=(base + 2) * 1.3,
            textColor=hx(c["text"]),
            spaceBefore=6,
            spaceAfter=3,
        ),
        "p": ParagraphStyle(
            "p",
            fontName=body,
            fontSize=base,
            leading=base * 1.4,
            textColor=hx(c["text"]),
            alignment=TA_LEFT,
            spaceAfter=4,
        ),
        "small": ParagraphStyle(
            "s",
            fontName=body,
            fontSize=base - 2,
            leading=(base - 2) * 1.35,
            textColor=hx(c["muted"]),
        ),
        "cell": ParagraphStyle(
            "c",
            fontName=body,
            fontSize=base - 2,
            leading=(base - 2) * 1.25,
            textColor=hx(c["text"]),
        ),
        "cellh": ParagraphStyle(
            "ch",
            fontName=bold,
            fontSize=base - 2,
            leading=(base - 2) * 1.25,
            textColor=hx(c["text"]),
        ),
        "code": ParagraphStyle(
            "code",
            fontName="Courier",
            fontSize=base - 2,
            leading=(base - 2) * 1.3,
            textColor=hx(c["text"]),
            backColor=hx(c["surface"]),
        ),
        "kpi": ParagraphStyle(
            "k",
            fontName=head_font,
            fontSize=sizes.get("title", 20) + 4,
            leading=(sizes.get("title", 20) + 4) * 1.15,
            textColor=hx(c["text"]),
        ),
    }

    def md(text: str) -> list[Any]:
        out: list[Any] = []
        for b in md_blocks(text):
            style = (
                st["h2"] if b.kind.startswith("h") else st["code"] if b.kind == "code" else st["p"]
            )
            bullet = None
            if b.kind == "li":
                indent = 12 * max(b.level, 1)
                style = ParagraphStyle(
                    "li",
                    parent=st["p"],
                    leftIndent=indent + 8,
                    bulletIndent=indent - 4,
                    spaceAfter=1,
                )
                head = b.runs[0].text if b.runs else ""
                if head == "• ":
                    b = MdBlock(b.kind, b.runs[1:], b.level)
                    bullet = "\u2013"  # en dash: safe in the base-14 WinAnsi fonts
                elif head.endswith(". ") and head[:-2].isdigit():
                    b = MdBlock(b.kind, b.runs[1:], b.level)
                    bullet = head.strip()
            out.append(Paragraph(_runs_markup(b), style, bulletText=bullet))
        return out

    def chart_flowable(spec: Mapping[str, Any], width: float) -> Any:
        drawing = svg2rlg(io.StringIO(chart_svg(spec)))
        if drawing is None or not drawing.width:
            return Paragraph("(chart could not be rendered)", st["small"])
        scale = min(width / drawing.width, 1.6)
        drawing.width *= scale
        drawing.height *= scale
        drawing.scale(scale, scale)
        return drawing

    def table_flowable(t: Mapping[str, Any], width: float) -> Any:
        cols = t["columns"]
        header = [Paragraph(escape(col["title"]), st["cellh"]) for col in cols]
        data = [header] + [
            [Paragraph(escape(str(v)), st["cell"]) for v in row] for row in t["rows"]
        ]
        if len(data) == 1:
            return Paragraph("No rows.", st["small"])
        tbl = LongTable(data, repeatRows=1, colWidths=[width / max(len(cols), 1)] * len(cols))
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), hx(c["surface"])),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, hx(c["border"])),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LINEBELOW", (0, 1), (-1, -1), 0.25, hx(c["grid"])),
        ]
        if t.get("zebra"):
            for i in range(2, len(data), 2):
                style.append(("BACKGROUND", (0, i), (-1, i), hx(c["surface"])))
        sc = t.get("status_column")
        if sc:
            idx = [col["name"] for col in cols].index(sc)
            for i, row in enumerate(t["rows"], start=1):
                col_hex = status_color(theme, row[idx])
                if col_hex:
                    style.append(("TEXTCOLOR", (idx, i), (idx, i), hx(col_hex)))
                    style.append(("LINEBEFORE", (idx, i), (idx, i), 3, hx(col_hex)))
        tbl.setStyle(TableStyle(style))
        return tbl

    def kpi_flowable(b: Mapping[str, Any], width: float) -> Any:
        k = b["kpi"]
        colour = {
            "good": theme["status"].get("passed"),
            "bad": theme["status"].get("exception"),
        }.get(str(k.get("status")))
        value_style = ParagraphStyle("kv", parent=st["kpi"], textColor=hx(colour or c["text"]))
        cell = [
            Paragraph(escape(k["display"]), value_style),
            Paragraph(escape(k["label"]), st["small"]),
        ]
        box = RLTable([[cell]], colWidths=[width - 6])
        box.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 0.6, hx(c["border"])),
                    ("BACKGROUND", (0, 0), (-1, -1), hx(c["surface"])),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        return box

    def block_flowables(b: Mapping[str, Any], width: float) -> list[Any]:
        out: list[Any] = []
        if b.get("title") and b["renderer"] != "kpi":
            out.append(Paragraph(escape(b["title"]), st["h2"]))
        r = b["renderer"]
        if r == "vega":
            out.append(chart_flowable(b["spec"], width))
        elif r == "kpi":
            out.append(kpi_flowable(b, width))
        elif r == "table":
            out.append(table_flowable(b["table"], width))
            if b["table"].get("truncated"):
                out.append(
                    Paragraph(
                        f"Showing {len(b['table']['rows'])} of {b['table']['total_rows']} rows.",
                        st["small"],
                    )
                )
        elif r in ("text", "callout"):
            items = md(b.get("markdown") or "")
            if r == "callout":
                tone = {"success": "passed", "warning": "warning", "danger": "exception"}.get(
                    str(b.get("tone")), ""
                )
                edge = theme["status"].get(tone) or c["accent"]
                box = RLTable([[items or [Paragraph("", st["p"])]]], colWidths=[width - 4])
                box.setStyle(
                    TableStyle(
                        [
                            ("LINEBEFORE", (0, 0), (0, -1), 3, hx(edge)),
                            ("BACKGROUND", (0, 0), (-1, -1), hx(c["surface"])),
                            ("LEFTPADDING", (0, 0), (-1, -1), 10),
                        ]
                    )
                )
                out.append(box)
            else:
                out.extend(items)
        else:
            out.append(Paragraph(escape(str(b.get("error") or "unavailable")), st["small"]))
        if b.get("caption"):
            out.append(Paragraph(escape(b["caption"]), st["small"]))
        if b.get("suppressed_cells"):
            out.append(Paragraph(f"{b['suppressed_cells']} small cell(s) suppressed.", st["small"]))
        out.append(Spacer(1, 8))
        return out

    width_frac = {"full": 1.0, "half": 0.5, "third": 1 / 3, "two_thirds": 2 / 3}

    story: list[Any] = [Paragraph(escape(model["title"]), st["title"])]
    if model.get("subtitle"):
        story.append(Paragraph(escape(model["subtitle"]), st["subtitle"]))
    story.extend(md(model.get("description") or ""))
    if model["options"].get("show_toc") and model["sections"]:
        story.append(Paragraph("Contents", st["h2"]))
        for i, s in enumerate(model["sections"], start=1):
            story.append(Paragraph(f"{i}. {escape(s['title'])}", st["p"]))
    for s in model["sections"]:
        if s.get("page_break_before"):
            story.append(PageBreak())
        else:
            story.append(CondPageBreak(1.6 * inch))
        story.append(Paragraph(escape(s["title"]), st["h1"]))
        story.extend(md(s.get("description") or ""))
        row: list[list[Any]] = []
        used = 0.0
        widths: list[float] = []

        def flush() -> None:
            nonlocal row, used, widths
            if not row:
                return
            if len(row) == 1 and widths[0] >= 0.99:
                story.extend(row[0])
            else:
                grid = RLTable([row], colWidths=[w * frame_w for w in widths])
                grid.setStyle(
                    TableStyle(
                        [
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("LEFTPADDING", (0, 0), (-1, -1), 2),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                        ]
                    )
                )
                story.append(KeepTogether([grid]))
            row, used, widths = [], 0.0, []

        for b in s["blocks"]:
            frac = width_frac.get(str(b.get("width")), 1.0)
            if b["renderer"] == "table":
                frac = 1.0
            if used + frac > 1.001:
                flush()
            row.append(block_flowables(b, frac * frame_w - 8))
            widths.append(frac)
            used += frac
        flush()
    if model["options"].get("show_provenance", True):
        story.append(CondPageBreak(1.2 * inch))
        story.append(Paragraph("Provenance", st["h2"]))
        prov = model["provenance"]
        lines = [
            f"<b>{escape(str(k).replace('_', ' '))}</b>: {escape(str(v))}"
            for k, v in prov.items()
            if v not in (None, "", 0) or k == "suppressed_cells"
        ]
        for line in lines:
            story.append(Paragraph(line, st["small"]))

    footer_text = f"{model['title']} — generated by Ovid2 Report Studio"

    def on_page(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFillColor(hx(c["background"]))
        canvas.rect(0, 0, size[0], size[1], stroke=0, fill=1)
        canvas.setFont(body, 7.5)
        canvas.setFillColor(hx(c["muted"]))
        canvas.drawString(margin, 0.35 * inch, footer_text[:140])
        canvas.drawRightString(size[0] - margin, 0.35 * inch, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        str(path),
        pagesize=size,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=0.7 * inch,
        title=model["title"],
        author="Ovid2 Report Studio",
        subject=str(model["provenance"].get("source_title") or ""),
    )
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return path


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("library module")
