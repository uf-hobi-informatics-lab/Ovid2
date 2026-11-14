"""Contract: the Report Studio catalogues are complete, consistent and safe.

``adr-report-studio.md``. Pins:

1. Every chart kind in ``resources/reports/charts.yaml`` has a builder in
   :data:`ovid2.reports.charts.BUILDERS` (or a native ``kpi`` / ``table``
   renderer) and vice versa; every kind's ``params`` is a valid JSON
   Schema whose ``x-field`` / ``x-fields`` declare column types.
2. Every source adapter has at least one template, and every template
   validates and renders (no ``error`` blocks) against a sample source,
   under every report theme.
3. Every report theme carries the full token set.
4. **Theme independence.** The report renderer (``report_render.js`` /
   ``.css``) never reads the web page's theme tokens or ``[data-theme]``,
   so a report looks the same under any page theme and in every export.
5. **Tier neutrality.** ``ovid2.reports`` (minus the executor-only
   ``render`` subpackage) imports nothing from the web / MCP / LLM tiers,
   pipeline code or the document / chart libraries.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

pytestmark = pytest.mark.contract

_COLUMN_TYPES = {"quantitative", "nominal", "ordinal", "temporal", "boolean", "text"}


def test_every_chart_kind_has_a_builder() -> None:
    from ovid2.reports.catalogue import list_charts
    from ovid2.reports.charts import BUILDERS

    kinds = {c.name: c for c in list_charts()}
    vega = {n for n, c in kinds.items() if c.renderer == "vega"}
    native = {n for n, c in kinds.items() if c.renderer in ("kpi", "table")}
    assert vega == set(BUILDERS), f"charts.yaml vs BUILDERS drift: {sorted(vega ^ set(BUILDERS))}"
    assert native, "expected the native kpi / table renderers"
    assert len(kinds) >= 25, "the chart library should stay rich (25+ kinds)"


def test_chart_params_are_valid_schemas() -> None:
    from ovid2.reports.catalogue import chart_categories, list_charts

    cats = set(chart_categories())
    for chart in list_charts():
        Draft202012Validator.check_schema(chart.params)
        assert chart.category in cats, chart.name
        assert chart.summary, chart.name
        for pname, allowed in chart.field_params().items():
            assert set(allowed) <= _COLUMN_TYPES, f"{chart.name}.{pname}: {allowed}"
        for req in chart.required:
            assert req in chart.params.get("properties", {}), (
                f"{chart.name}: required {req!r} undefined"
            )


def test_every_source_kind_has_a_template_that_renders() -> None:
    from ovid2.reports import catalogue, layout
    from ovid2.reports.compose import render_model
    from ovid2.reports.samples import sample_sources
    from ovid2.reports.sources import ADAPTERS

    samples = sample_sources()
    assert set(samples) == set(ADAPTERS), "samples.sample_sources must cover every adapter"
    for kind in ADAPTERS:
        assert catalogue.list_templates(kind), f"source kind {kind!r} has no template"
    themes = [t.name for t in catalogue.list_themes()]
    for tmpl in catalogue.list_templates():
        source = samples[tmpl.source_kind]
        norm = layout.normalise(tmpl.layout)
        errs = layout.validate_layout(norm, source)
        assert not errs, f"{tmpl.template_id}: {errs}"
        for name in themes:
            model = render_model({**norm, "theme": name}, source, width=600)
            broken = [
                b["id"] for s in model["sections"] for b in s["blocks"] if b["renderer"] == "error"
            ]
            assert not broken, f"{tmpl.template_id} under {name}: {broken}"
        for rec in source.get("recommended_templates") or []:
            assert catalogue.get_template(rec) is not None, (
                f"{source['kind']} recommends unknown {rec}"
            )


def test_themes_are_complete() -> None:
    from ovid2.reports.catalogue import DEFAULT_THEME, list_themes

    themes = list_themes()
    assert DEFAULT_THEME in {t.name for t in themes}
    needed_colors = {"background", "surface", "text", "muted", "border", "accent", "grid"}
    for t in themes:
        assert needed_colors <= set(t.colors), f"{t.name}: missing {needed_colors - set(t.colors)}"
        assert len(t.palette) >= 6, t.name
        assert {"passed", "exception", "error"} <= set(t.status), t.name
        assert t.mode in ("light", "dark"), t.name
        for v in [*t.colors.values(), *t.palette, *t.status.values()]:
            assert re.fullmatch(r"#[0-9a-fA-F]{6}", v), f"{t.name}: {v}"


_PAGE_TOKENS = re.compile(
    r"var\(--(?!ovr-)[a-z]|data-theme|html\[|:root|getComputedStyle|Ovid2Theme|Ovid2Appearance"
)


def test_report_renderer_is_page_theme_independent(src_root: Path) -> None:
    static = src_root / "web" / "static"
    for name in ("report_render.js", "report_render.css"):
        text = (static / name).read_text(encoding="utf-8")
        code = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        hits = sorted(set(_PAGE_TOKENS.findall(code)))
        assert not hits, f"{name} reads page-theme state: {hits}"


_BANNED_IMPORTS = (
    "ovid2.web",
    "ovid2.mcp",
    "ovid2.llm",
    "ovid2.core",
    "ovid2.transformations",
    "ovid2.drivers",
    "ovid2.connections",
    "reportlab",
    "docx",
    "pptx",
    "xlsxwriter",
    "vl_convert",
    "svglib",
    "pyspark",
    "duckdb",
)


def test_reports_package_is_tier_neutral(src_root: Path) -> None:
    offenders: list[str] = []
    for py in (src_root / "reports").rglob("*.py"):
        if "render" in py.relative_to(src_root / "reports").parts:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods = [node.module]
            for m in mods:
                if any(m == b or m.startswith(b + ".") for b in _BANNED_IMPORTS):
                    offenders.append(f"{py.relative_to(src_root)}: {m}")
    assert not offenders, "ovid2.reports must stay tier-neutral:\n" + "\n".join(offenders)


def test_layout_ops_vocabulary_is_documented() -> None:
    from ovid2.reports.layout import OPS

    doc = (Path(__file__).resolve().parents[2] / "src" / "ovid2" / "resources" / "manual").rglob(
        "*.md"
    )
    text = "\n".join(p.read_text(encoding="utf-8") for p in doc)
    missing = [op for op in OPS if f"`{op}`" not in text]
    assert not missing, f"layout edit ops missing from the manual: {missing}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
