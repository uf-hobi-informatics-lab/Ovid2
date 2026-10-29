"""Phase 1 — storage-account & warehouse presentation tests.

The web panel consolidates the pre-existing Connections, Engines, and
Addresses resources into two user-facing concepts:

- **Storage accounts** (``/storage``) — backed by connection configs.
- **Warehouses** (``/warehouses``) — backed by engine configs.

The underlying on-disk model (connections / engines / addresses JSON,
drivers, stagers) is unchanged; only presentation is consolidated.
Legacy URLs redirect to the new canonical pages.
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_storage_page_renders(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/storage")
    assert r.status_code == 200
    assert "Storage accounts" in r.text
    # The create link still targets the connection CRUD.
    assert "/connections/new" in r.text


@pytest.mark.unit
def test_storage_page_renders_grouped_boxes(web_client, isolated_xdg) -> None:  # type: ignore[no-untyped-def]
    """Storage accounts render as stacked boxes grouped by storage category."""
    conndir = isolated_xdg / "config" / "ovid2" / "connections"
    conndir.mkdir(parents=True, exist_ok=True)
    (conndir / "st_disc.json").write_text(
        '{"type": "disc", "args": {"host_path": "/tmp", "container_path": "/data", "mount_mode": "rw"}}',
        encoding="utf-8",
    )
    html = web_client.get("/storage").text
    assert 'class="page pl-page"' in html
    assert 'class="pl-stats"' in html
    assert 'data-storage-group="filesystem"' in html
    # Only categories with accounts are rendered.
    assert 'data-storage-group="database"' not in html
    assert 'class="pl-grid"' in html
    assert 'data-connection-row="st_disc" data-ctype="disc"' in html
    assert 'class="pl-card"' in html and 'class="pl-flow"' in html
    # Hooks used by the inline Test / Test-all script survive the redesign,
    # and the click handler tolerates clicks on the inline SVG icon.
    assert 'data-test-cell="st_disc"' in html
    assert 'data-test-connection="st_disc"' in html
    assert 'data-test-all="connections"' in html
    assert 'closest("[data-test-connection]")' in html
    assert "<table" not in html.split('id="test-all-summary"')[1].split("</section>")[0]


@pytest.mark.unit
def test_storage_page_empty_state(web_client, isolated_xdg) -> None:  # type: ignore[no-untyped-def]
    html = web_client.get("/storage").text
    assert 'class="pl-empty"' in html
    assert "No storage accounts yet" in html


@pytest.mark.unit
def test_warehouses_page_renders(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/warehouses")
    assert r.status_code == 200
    assert "Warehouses" in r.text
    assert "/engines/new" in r.text


@pytest.mark.unit
def test_warehouses_page_renders_stacked_boxes(web_client, isolated_xdg) -> None:  # type: ignore[no-untyped-def]
    """Each warehouse renders as a stacked component box (Pipelines look)."""
    r = web_client.post(
        "/api/v1/engines/wh_box",
        json={"type": "pyspark", "args": {"spark.executor.memory": "8g"}, "secrets": {}},
    )
    assert r.status_code == 201
    html = web_client.get("/warehouses").text
    assert 'class="pl-grid"' in html
    assert 'data-engine-row="wh_box"' in html and 'data-etype="pyspark"' in html
    assert 'class="pl-card"' in html and 'class="pl-flow"' in html
    # Hooks used by the inline Test / Test-all script survive the redesign.
    assert 'data-test-cell="wh_box"' in html
    assert 'data-test-engine="wh_box"' in html
    assert 'data-test-all="engines"' in html
    assert "<table" not in html.split('id="test-all-summary"')[1].split("</section>")[0]


@pytest.mark.unit
def test_connections_redirects_to_storage(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/connections", follow_redirects=False)
    assert r.status_code in (307, 308)
    assert r.headers["location"] == "/storage"


@pytest.mark.unit
def test_engines_redirects_to_warehouses(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/engines", follow_redirects=False)
    assert r.status_code in (307, 308)
    assert r.headers["location"] == "/warehouses"


@pytest.mark.unit
def test_addresses_redirects_to_storage(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/addresses", follow_redirects=False)
    assert r.status_code in (307, 308)
    assert r.headers["location"] == "/storage"


@pytest.mark.unit
def test_pipelines_page_renders(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/pipelines")
    assert r.status_code == 200
    assert "Pipelines" in r.text
    assert "/pipelines/new" in r.text


@pytest.mark.unit
def test_routes_redirects_to_pipelines(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/routes", follow_redirects=False)
    assert r.status_code in (307, 308)
    assert r.headers["location"] == "/pipelines"


@pytest.mark.unit
def test_nav_shows_pipelines_not_routes(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/")
    assert r.status_code == 200
    body = r.text
    assert ">Pipelines<" in body
    assert ">Routes<" not in body


@pytest.mark.unit
def test_nav_drops_connections_engines_addresses(web_client) -> None:  # type: ignore[no-untyped-def]
    """The drawer nav shows the consolidated concepts, not the old three."""
    r = web_client.get("/")
    assert r.status_code == 200
    body = r.text
    assert ">Storage accounts<" in body
    assert ">Warehouses<" in body
    # The old nav labels are gone.
    assert ">Connections<" not in body
    assert ">Engines<" not in body
    assert ">Addresses<" not in body


@pytest.mark.unit
def test_warehouse_new_form_relabelled(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/engines/new?type=snowpark")
    assert r.status_code == 200
    assert "New warehouse" in r.text
    assert "Warehouse type" in r.text


@pytest.mark.unit
def test_storage_new_form_relabelled(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/connections/new?type=s3")
    assert r.status_code == 200
    assert "New storage account" in r.text
    assert "Storage type" in r.text
