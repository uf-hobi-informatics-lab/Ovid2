"""Unit tests — pipeline builder + transform library HTML pages (builder B)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_STATIC = Path(__file__).resolve().parents[3] / "src" / "ovid2" / "web" / "static"


def test_builder_page_renders_tree_and_editor(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/pipeline-packages/example_pcornet/builder")
    assert r.status_code == 200, r.text
    body = r.text
    assert 'id="spec-builder"' in body and 'data-pipeline="example_pcornet"' in body
    for doc in ("checks", "assignments", "transforms", "fixers", "settings"):
        assert f'data-doc="{doc}"' in body
    assert 'data-table="demographic"' in body
    assert 'id="pb-inspection"' in body  # initial inspection JSON
    assert 'data-tab="form"' in body and 'data-tab="yaml"' in body
    assert "/static/spec_builder.js" in body


def test_builder_page_404_for_unknown_package(web_client) -> None:  # type: ignore[no-untyped-def]
    assert web_client.get("/pipeline-packages/nope_pkg/builder").status_code == 404


def test_transforms_library_page(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/transforms")
    assert r.status_code == 200
    assert 'data-mode="library"' in r.text
    assert 'data-nav="transforms"' in r.text
    assert 'href="/transforms" class="nav-item active"' in r.text


def test_customize_links(web_client) -> None:  # type: ignore[no-untyped-def]
    body = web_client.get("/pipeline-packages").text
    assert 'href="/pipeline-packages/example_pcornet/builder"' in body
    assert "/pipelines/example_pcornet/config" not in body  # old 404 link
    hub = web_client.get("/pipeline-packages/example_pcornet").text
    assert "/pipeline-packages/example_pcornet/builder" in hub


def test_static_modules_served_and_offline(web_client) -> None:  # type: ignore[no-untyped-def]
    for name in (
        "spec_builder.js",
        "yaml_editor.js",
        "flow_graph.js",
        "vendor/codemirror.bundle.js",
    ):
        r = web_client.get(f"/static/{name}")
        assert r.status_code == 200, name
    for name in ("spec_builder.js", "yaml_editor.js", "flow_graph.js", "pipeline_builder.js"):
        text = (_STATIC / name).read_text(encoding="utf-8")
        assert not re.search(r"""import[^;]*["']https?://""", text), f"{name} imports from a CDN"
    bundle = (_STATIC / "vendor" / "codemirror.bundle.js").read_text(encoding="utf-8")
    assert "export{" in bundle.replace(" ", "")
    assert not re.search(r"""from\s*["']https?://""", bundle)
