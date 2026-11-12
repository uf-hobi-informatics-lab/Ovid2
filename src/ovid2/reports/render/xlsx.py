"""XLSX export — one sheet per visible data block plus a provenance sheet."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = ["write_xlsx"]

_BAD = re.compile(r"[\[\]:*?/\\]")


def _sheet_name(title: str, taken: set[str]) -> str:
    base = _BAD.sub("_", title).strip("'")[:28] or "Sheet"
    name = base
    n = 2
    while name.lower() in taken:
        name = f"{base[:26]}_{n}"
        n += 1
    taken.add(name.lower())
    return name


def _cell(value: Any) -> Any:
    """Neutralise spreadsheet formula injection in text cells."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def write_xlsx(model: Mapping[str, Any], path: Path) -> Path:
    """Write every visible data block's (filtered, suppressed) rows."""
    import xlsxwriter

    theme = model["theme"]
    c = theme["colors"]
    wb = xlsxwriter.Workbook(str(path), {"strings_to_formulas": False, "strings_to_urls": False})
    head = wb.add_format(
        {
            "bold": True,
            "bg_color": c["surface"],
            "font_color": c["text"],
            "border": 1,
            "border_color": c["border"],
        }
    )
    title_fmt = wb.add_format({"bold": True, "font_size": 14})
    muted = wb.add_format({"italic": True, "font_color": c["muted"]})
    taken: set[str] = set()

    index = wb.add_worksheet(_sheet_name("Report", taken))
    index.write(0, 0, model["title"], title_fmt)
    index.write(1, 0, model.get("subtitle") or "", muted)
    index.write_row(3, 0, ["Section", "Block", "Type", "Sheet", "Rows", "Suppressed cells"], head)
    r = 4
    for s in model["sections"]:
        for b in s["blocks"]:
            data = b.get("data")
            sheet_name = ""
            if data and data.get("rows") is not None:
                sheet_name = _sheet_name(b.get("title") or b["id"], taken)
                ws = wb.add_worksheet(sheet_name)
                cols = data["columns"]
                ws.write_row(0, 0, [col.get("title") or col["name"] for col in cols], head)
                for i, row in enumerate(data["rows"], start=1):
                    ws.write_row(i, 0, [_cell(row.get(col["name"])) for col in cols])
                ws.freeze_panes(1, 0)
                ws.autofilter(0, 0, max(len(data["rows"]), 1), max(len(cols) - 1, 0))
                for j, col in enumerate(cols):
                    ws.set_column(
                        j, j, min(max(len(str(col.get("title") or col["name"])) + 2, 10), 40)
                    )
            index.write_row(
                r,
                0,
                [
                    s["title"],
                    b.get("title") or b["id"],
                    b.get("chart") or b["type"],
                    sheet_name,
                    b.get("row_count", ""),
                    b.get("suppressed_cells", 0),
                ],
            )
            if sheet_name:
                index.write_url(r, 3, f"internal:'{sheet_name}'!A1", string=sheet_name)
            r += 1
    index.set_column(0, 1, 32)
    index.set_column(2, 5, 16)

    prov = wb.add_worksheet(_sheet_name("Provenance", taken))
    prov.write_row(0, 0, ["Field", "Value"], head)
    for i, (k, v) in enumerate(model["provenance"].items(), start=1):
        prov.write(i, 0, str(k))
        prov.write(i, 1, _cell("" if v is None else str(v)))
    prov.set_column(0, 0, 24)
    prov.set_column(1, 1, 60)
    wb.set_properties({"title": model["title"], "author": "Ovid2 Report Studio"})
    wb.close()
    return path


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("library module")
