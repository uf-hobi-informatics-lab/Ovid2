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
def test_warehouses_page_renders(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/warehouses")
    assert r.status_code == 200
    assert "Warehouses" in r.text
    assert "/engines/new" in r.text


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
