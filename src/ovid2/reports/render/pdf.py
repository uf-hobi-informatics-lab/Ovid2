"""PDF export (reportlab; charts embedded as vector graphics via svglib)."""

from __future__ import annotations

import io
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from ovid2.reports.render._common import (
    CHECK_OFF,
    CHECK_ON,
    FONT_SCALE,
    PADDING_PT,
    SPACER_PT,
    MdBlock,
    boxed,
    chart_svg,
    md_blocks,
    note_byline,
    note_tone_color,
    status_color,
)

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
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4, LEGAL, LETTER, landscape, portrait
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        CondPageBreak,
        HRFlowable,
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

    def md(text: str, blk: Mapping[str, Any] | None = None) -> list[Any]:
        out: list[Any] = []
        for b in md_blocks(text):
            name = "h2" if b.kind.startswith("h") else "code" if b.kind == "code" else "p"
            style = styled(name, blk) if blk else st[name]
            bullet = None
            if b.kind == "li":
                indent = 12 * max(b.level, 1)
                style = ParagraphStyle(
                    "li",
                    parent=styled("p", blk) if blk else st["p"],
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

    def styled(name: str, b: Mapping[str, Any] | None) -> Any:
        """Return ``st[name]`` adjusted for the block's style overrides."""
        kw = _style_kw(b, st[name])
        if not kw:
            return st[name]
        return ParagraphStyle(f"{name}_{(b or {}).get('id')}", parent=st[name], **kw)

    def md_styled(text: str, b: Mapping[str, Any]) -> list[Any]:
        return md(text, b if b.get("style_resolved") else None)

    def _style_kw(b: Mapping[str, Any] | None, parent: Any) -> dict[str, Any]:
        sr = (b or {}).get("style_resolved") or {}
        kw: dict[str, Any] = {}
        if sr.get("text_color"):
            kw["textColor"] = hx(sr["text_color"])
        align = {"center": TA_CENTER, "right": TA_RIGHT}.get(str(sr.get("align")))
        if align is not None:
            kw["alignment"] = align
        scale = FONT_SCALE.get(str(sr.get("font_scale")), 1.0)
        if scale != 1.0:
            kw["fontSize"] = parent.fontSize * scale
            kw["leading"] = parent.leading * scale
        return kw

    def box(items: list[Any], b: Mapping[str, Any], width: float, edge: str | None = None) -> Any:
        """Wrap ``items`` in the block's card / outline / fill."""
        sr = b.get("style_resolved") or {}
        pad = PADDING_PT.get(str(sr.get("padding")), 8)
        emphasis = sr.get("emphasis") or ("filled" if b.get("renderer") == "note" else "card")
        bg = sr.get("background") or (c["surface"] if emphasis in ("card", "filled") else None)
        border_w = {"none": 0, "subtle": 0.5, "strong": 1.4}.get(str(sr.get("border")))
        if border_w is None:
            border_w = 0.6 if emphasis in ("card", "outlined") else 0
        cmds: list[Any] = [
            ("LEFTPADDING", (0, 0), (-1, -1), pad + (4 if edge else 0)),
            ("RIGHTPADDING", (0, 0), (-1, -1), pad),
            ("TOPPADDING", (0, 0), (-1, -1), pad * 0.75),
            ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
        ]
        if bg:
            cmds.append(("BACKGROUND", (0, 0), (-1, -1), hx(bg)))
        if border_w:
            cmds.append(("BOX", (0, 0), (-1, -1), border_w, hx(sr.get("accent") or c["border"])))
        if edge:
            cmds.append(("LINEBEFORE", (0, 0), (0, -1), 3, hx(edge)))
        tbl = RLTable([[items or [Paragraph("", st["p"])]]], colWidths=[width - 4])
        tbl.setStyle(TableStyle(cmds))
        return tbl

    def free_flowables(b: Mapping[str, Any], width: float) -> list[Any]:
        r = b["renderer"]
        if r == "divider":
            sr = b.get("style_resolved") or {}
            weight = {"none": 0.3, "subtle": 0.5, "strong": 1.6}.get(str(sr.get("border")), 0.8)
            return [
                HRFlowable(
                    width="100%",
                    thickness=weight,
                    color=hx(sr.get("accent") or c["border"]),
                    spaceBefore=6,
                    spaceAfter=6,
                )
            ]
        if r == "spacer":
            return [Spacer(1, SPACER_PT.get(str(b.get("size")), 18))]
        if r == "info":
            rows = b.get("rows") or []
            if not rows:
                return []
            key_style = ParagraphStyle(
                f"ik_{b['id']}", parent=styled("cellh", b), textColor=hx(c["muted"])
            )
            data = [
                [
                    Paragraph(escape(row["key"]), key_style),
                    Paragraph(escape(row["value"]) or "&nbsp;", styled("p", b)),
                ]
                for row in rows
            ]
            tbl = RLTable(data, colWidths=[width * 0.34, width * 0.66 - 4])
            tbl.setStyle(
                TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LINEBELOW", (0, 0), (-1, -2), 0.3, hx(c["grid"])),
                        ("TOPPADDING", (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ]
                )
            )
            return [tbl]
        if r == "checklist":
            out: list[Any] = []
            for item in b.get("items") or []:
                mark = CHECK_ON if item["done"] else CHECK_OFF
                text = escape(item["text"])
                if item["done"]:
                    text = f"<strike>{text}</strike>"
                out.append(
                    Paragraph(
                        f'<font face="Courier">{mark}</font>&nbsp;&nbsp;{text}', styled("p", b)
                    )
                )
            return out
        # note
        items = [Paragraph(escape(note_byline(b)), st["small"])]
        items.extend(md_styled(b.get("markdown") or "", b))
        return [box(items, b, width, edge=note_tone_color(theme, b))]

    def block_flowables(b: Mapping[str, Any], width: float) -> list[Any]:
        out: list[Any] = []
        r = b["renderer"]
        if b.get("title") and r not in ("kpi", "divider", "spacer"):
            out.append(Paragraph(escape(b["title"]), styled("h2", b)))
        content: list[Any] = []
        if r in ("note", "info", "checklist", "divider", "spacer"):
            content.extend(free_flowables(b, width))
        elif r == "vega":
            content.append(chart_flowable(b["spec"], width))
        elif r == "kpi":
            content.append(kpi_flowable(b, width))
        elif r == "table":
            content.append(table_flowable(b["table"], width))
            if b["table"].get("truncated"):
                content.append(
                    Paragraph(
                        f"Showing {len(b['table']['rows'])} of {b['table']['total_rows']} rows.",
                        st["small"],
                    )
                )
        elif r in ("text", "callout"):
            items = md_styled(b.get("markdown") or "", b)
            if r == "callout":
                content.append(box(items, b, width, edge=note_tone_color(theme, b)))
            else:
                content.extend(items)
        else:
            content.append(Paragraph(escape(str(b.get("error") or "unavailable")), st["small"]))
        if r not in ("note", "callout") and boxed(b):
            content = [box(content, b, width)]
        out.extend(content)
        if b.get("caption"):
            out.append(Paragraph(escape(b["caption"]), st["small"]))
        if b.get("suppressed_cells"):
            out.append(Paragraph(f"{b['suppressed_cells']} small cell(s) suppressed.", st["small"]))
        if r != "spacer":
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
        if s.get("kind") == "notes":
            story.append(
                HRFlowable(
                    width="18%", thickness=1.6, color=hx(c["accent"]), hAlign="LEFT", spaceAfter=6
                )
            )
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
