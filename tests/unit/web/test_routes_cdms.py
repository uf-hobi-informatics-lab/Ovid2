"""Route tests for the CDMs pages and REST endpoints (C1 UI)."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.unit
def test_api_list_cdms_returns_typed_summaries(web_client, isolated_xdg: Path) -> None:
    """``GET /api/v1/cdms`` returns typed summary objects."""
    r = web_client.get("/api/v1/cdms")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    names = {row["name"] for row in data}
    assert "pcornet_7_0" in names
    pcornet = next(row for row in data if row["name"] == "pcornet_7_0")
    assert pcornet.keys() == {"name", "version", "table_count", "has_yaml"}
    assert pcornet["has_yaml"] is True
    assert pcornet["table_count"] > 0


@pytest.mark.unit
def test_api_get_cdm_pcornet_ok(web_client, isolated_xdg: Path) -> None:
    """``GET /api/v1/cdms/pcornet_7_0`` returns full detail."""
    r = web_client.get("/api/v1/cdms/pcornet_7_0")
    assert r.status_code == 200
    d = r.json()
    assert d["name"] == "pcornet_7_0"
    assert d["version"] == "7.0"
    assert d["has_yaml"] is True
    assert isinstance(d["tables"], list) and d["tables"]
    assert any(t["name"] == "demographic" for t in d["tables"])
    assert isinstance(d["valuesets"], list) and d["valuesets"]


@pytest.mark.unit
def test_api_get_cdm_404(web_client, isolated_xdg: Path) -> None:
    """Unknown CDM returns 404 with a helpful message."""
    r = web_client.get("/api/v1/cdms/no_such_cdm")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


@pytest.mark.unit
def test_html_cdms_index_ok(web_client, isolated_xdg: Path) -> None:
    """``GET /cdms`` renders the list page with every CDM name."""
    r = web_client.get("/cdms")
    assert r.status_code == 200
    body = r.text
    assert "Common Data Models" in body
    assert "pcornet_7_0" in body
    assert "ovid2_pipeline_nursing_v1" in body


@pytest.mark.unit
def test_html_cdms_index_links_to_detail(web_client, isolated_xdg: Path) -> None:
    """The index page links each row to ``/cdms/{name}``."""
    r = web_client.get("/cdms")
    assert r.status_code == 200
    assert 'href="/cdms/pcornet_7_0"' in r.text


@pytest.mark.unit
def test_html_cdm_detail_ok(web_client, isolated_xdg: Path) -> None:
    """``GET /cdms/pcornet_7_0`` renders tables + valuesets."""
    r = web_client.get("/cdms/pcornet_7_0")
    assert r.status_code == 200
    body = r.text
    assert "pcornet_7_0" in body
    # A well-known table + a well-known valueset column.
    assert "demographic" in body
    assert "Tables &amp; composite keys" in body
    assert "Valuesets" in body


@pytest.mark.unit
def test_html_cdm_detail_404(web_client, isolated_xdg: Path) -> None:
    """Unknown CDM name returns 404 for the HTML detail page."""
    r = web_client.get("/cdms/no_such_cdm")
    assert r.status_code == 404


@pytest.mark.unit
def test_nav_has_cdms_link(web_client, isolated_xdg: Path) -> None:
    """The Configure nav group includes a CDMs entry pointing to /cdms."""
    r = web_client.get("/")
    assert r.status_code == 200
    assert 'href="/cdms"' in r.text
    assert ">CDMs<" in r.text
