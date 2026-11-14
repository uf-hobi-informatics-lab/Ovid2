"""``ovid2 tools report-export``: every format renders, hidden content never leaks."""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from ovid2.reports import catalogue, layout
from ovid2.reports.samples import sample_sources

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    for mod in ("vl_convert", "svglib", "reportlab", "docx"):
        pytest.importorskip(mod)
    src = sample_sources()["health"]
    tmpl = catalogue.get_template("health.edc_summary")
    assert tmpl is not None
    lay, _ = layout.apply_ops(
        layout.normalise(tmpl.layout),
        [
            {"op": "add_section", "title": "HIDDENSECTIONTITLE", "section_id": "secret"},
            {
                "op": "add_block",
                "section_id": "secret",
                "block": {"type": "text", "text": "HIDDENBODY"},
            },
            {"op": "set_visibility", "target_id": "secret", "visible": False},
            {
                "op": "update_block",
                "block_id": "status_donut",
                "set": {"title": "HIDDENBLOCK", "visible": False},
            },
        ],
        src,
    )
    path = tmp_path_factory.mktemp("bundle") / "bundle.json"
    path.write_text(
        json.dumps(
            {"layout": lay, "source": src, "meta": {"report_id": "rpt_test", "report_version": 3}}
        )
    )
    return path


def _export(bundle: Path, fmt: str, out: Path, *extra: str) -> dict:
    out.mkdir(exist_ok=True)
    p = subprocess.run(
        [
            sys.executable,
            "-m",
            "ovid2",
            "tools",
            "report-export",
            "--bundle",
            str(bundle),
            "--format",
            fmt,
            "--out-dir",
            str(out),
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    result = json.loads(p.stdout.strip().splitlines()[-1])
    assert result["ok"], result
    path = Path(result["path"])
    assert path.is_file() and path.stat().st_mode & 0o777 == 0o600
    return result


def _pdf_text(path: Path) -> str:
    """Return the text literals of a reportlab PDF (ASCII85 + Flate streams)."""
    import base64
    import re
    import zlib

    raw = path.read_bytes()
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", raw, re.S):
        data = m.group(1).strip()
        try:
            if data.endswith(b"~>"):
                data = base64.a85decode(data[:-2].replace(b"\n", b""))
            out.append(zlib.decompress(data).decode("latin-1"))
        except (ValueError, zlib.error):
            out.append(data.decode("latin-1"))
    return "".join(re.findall(r"\(((?:[^()\\]|\\.)*)\)", "".join(out)))


def _pptx_texts(prs: Any) -> list[str]:
    out: list[str] = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                out.append(shape.text_frame.text)
            if shape.has_table:
                out.extend(c.text for row in shape.table.rows for c in row.cells)
    return out


def _text_of(fmt: str, path: Path) -> str:
    if fmt == "pdf":
        return _pdf_text(path)
    if fmt == "docx":
        import docx

        return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
    if fmt == "pptx":
        import pptx

        return "\n".join(_pptx_texts(pptx.Presentation(str(path))))
    if fmt == "xlsx":
        import openpyxl

        wb = openpyxl.load_workbook(str(path))
        return "\n".join(
            ws.title
            + " "
            + " ".join(str(c.value) for row in ws.iter_rows() for c in row if c.value is not None)
            for ws in wb
        )
    if fmt in ("md", "svg", "png") and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            return "\n".join(
                z.read(n).decode("utf-8", "replace") for n in z.namelist() if not n.endswith(".png")
            )
    return path.read_text(encoding="utf-8", errors="replace")


@pytest.mark.parametrize("fmt", ["pdf", "html", "docx", "pptx", "xlsx", "md", "svg", "png"])
def test_format_renders_and_omits_hidden(bundle: Path, tmp_path: Path, fmt: str) -> None:
    res = _export(bundle, fmt, tmp_path / fmt)
    path = Path(res["path"])
    assert res["size"] > 200
    if fmt == "pdf":
        assert path.read_bytes().startswith(b"%PDF")
    if fmt == "png" and not zipfile.is_zipfile(path):
        assert path.read_bytes().startswith(b"\x89PNG")
    text = _text_of(fmt, path)
    for needle in ("HIDDENSECTIONTITLE", "HIDDENBODY", "HIDDENBLOCK"):
        assert needle not in text, f"{fmt} leaked {needle}"
    if fmt in ("html", "docx", "pptx", "md", "pdf"):
        assert "Data-characterisation summary" in text or "Data-characterisation" in text


def test_html_is_offline(bundle: Path, tmp_path: Path) -> None:
    res = _export(bundle, "html", tmp_path / "h")
    html = Path(res["path"]).read_text(encoding="utf-8")
    assert "<script src=" not in html and "cdn." not in html.lower()
    assert "window.vegaEmbed" in html and "Ovid2ReportRender" in html


def test_pptx_is_a_widescreen_deck(bundle: Path, tmp_path: Path) -> None:
    pptx = pytest.importorskip("pptx")
    res = _export(bundle, "pptx", tmp_path / "p")
    assert res["media_type"].endswith("presentationml.presentation")
    prs = pptx.Presentation(res["path"])
    assert round(prs.slide_width / prs.slide_height, 2) == round(16 / 9, 2)
    slides = list(prs.slides)
    assert len(slides) >= 3  # title + content + provenance
    pictures = [s for sl in slides for s in sl.shapes if s.shape_type == 13]
    assert pictures, "charts should be embedded as pictures"
    texts = _pptx_texts(prs)
    assert any("Provenance" in t for t in texts)
    for sl in slides:  # every shape stays on the slide
        for shp in sl.shapes:
            assert shp.left >= 0 and shp.top >= 0
            assert shp.left + shp.width <= prs.slide_width + 10
            assert shp.top + shp.height <= prs.slide_height + 10


def test_pptx_row_packing() -> None:
    from ovid2.reports.render.pptx import pack_rows

    blocks = [
        {"renderer": "kpi", "width": "third"},
        {"renderer": "kpi", "width": "third"},
        {"renderer": "kpi", "width": "third"},
        {"renderer": "vega", "width": "half"},
        {"renderer": "table", "width": "half"},
        {"renderer": "vega", "width": "half"},
    ]
    rows = pack_rows(blocks)
    assert [len(r) for r in rows] == [3, 1, 1, 1]
    assert rows[2][0][1] == 1.0  # tables take a full row


def test_xlsx_has_provenance_sheet(bundle: Path, tmp_path: Path) -> None:
    import openpyxl

    res = _export(bundle, "xlsx", tmp_path / "x")
    wb = openpyxl.load_workbook(res["path"])
    assert any("rovenance" in ws.title for ws in wb)


def test_single_chart_svg(bundle: Path, tmp_path: Path) -> None:
    res = _export(bundle, "svg", tmp_path / "one", "--block", "parts_stack")
    assert res["media_type"] == "image/svg+xml"
    assert Path(res["path"]).read_text(encoding="utf-8").lstrip().startswith("<svg")


def test_invalid_layout_is_refused(tmp_path: Path) -> None:
    src = sample_sources()["health"]
    bad = {
        "layout": {
            "v": 1,
            "title": "T",
            "sections": [
                {"id": "s", "title": "S", "blocks": [{"id": "b", "type": "chart", "chart": "nope"}]}
            ],
        },
        "source": src,
        "meta": {},
    }
    path = tmp_path / "b.json"
    path.write_text(json.dumps(bad))
    p = subprocess.run(
        [
            sys.executable,
            "-m",
            "ovid2",
            "tools",
            "report-export",
            "--bundle",
            str(path),
            "--format",
            "json",
            "--out-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert p.returncode == 1
    assert "layout invalid" in json.loads(p.stdout.strip().splitlines()[-1])["message"]
