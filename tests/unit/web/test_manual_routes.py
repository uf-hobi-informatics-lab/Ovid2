"""Route tests for the in-panel manual (``/manual``)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_manual_home_renders_in_shell(web_client: object) -> None:
    response = web_client.get("/manual")  # type: ignore[attr-defined]
    assert response.status_code == 200
    body = response.text
    assert 'href="/manual" class="nav-item active"' in body
    assert "/static/manual.css" in body and "/static/manual.js" in body
    assert "The Ovid2 Manual" in body and 'class="om-index"' in body


def test_manual_page_and_chapter_redirect(web_client: object) -> None:
    page = web_client.get("/manual/02-concepts/storage")  # type: ignore[attr-defined]
    assert page.status_code == 200
    assert 'href="/manual/02-concepts/dataspace"' in page.text or "/manual/" in page.text
    assert 'class="om-toc"' in page.text
    redirect = web_client.get(  # type: ignore[attr-defined]
        "/manual/02-concepts", follow_redirects=False
    )
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/manual/02-concepts/index"


def test_manual_404s(web_client: object) -> None:
    assert web_client.get("/manual/nope/page").status_code == 404  # type: ignore[attr-defined]
    assert web_client.get("/manual/nope").status_code == 404  # type: ignore[attr-defined]


def test_manual_assets_are_confined(web_client: object) -> None:
    ok = web_client.get("/manual/assets/05-five-stage.svg")  # type: ignore[attr-defined]
    assert ok.status_code == 200 and ok.headers["content-type"].startswith("image/svg")
    bad = web_client.get("/manual/assets/..%2Fmanual.yaml")  # type: ignore[attr-defined]
    assert bad.status_code == 404


def test_manual_search_index(web_client: object) -> None:
    response = web_client.get("/manual/search.json")  # type: ignore[attr-defined]
    data = response.json()
    assert data["v"] == 1 and len(data["docs"]) > 100
    assert any(d["u"].startswith("04-running-jobs/overwrite-and-batches") for d in data["docs"])


def test_manual_in_nav_on_every_page(web_client: object) -> None:
    body = web_client.get("/").text  # type: ignore[attr-defined]
    assert 'href="/manual" class="nav-item ' in body and 'data-nav="manual"' in body
