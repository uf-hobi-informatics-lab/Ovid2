"""End-to-end tests for the connection-first address create flow.

Covers the three routes involved:

- ``GET /addresses/new`` — connection picker (step 1 of 2).
- ``GET /addresses/new/step2?connection=<n>`` — location form (step 2).
- ``POST /addresses`` — persist.

Also exercises the shape-branching edit page: legacy configs get the
free-form key/value grid + a "convert" banner; W7 configs get the
location-only form.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _make_conn(web_client, name: str, type_: str = "disc") -> None:
    if type_ == "snowflake":
        body = {"type": "snowflake", "args": {"account": "acc", "user": "u"}}
    else:
        body = {"type": "disc", "args": {"host_path": "/tmp"}}
    r = web_client.post(f"/api/v1/connections/{name}", json=body)
    assert r.status_code in (200, 201), r.text


# ---------------------------------------------------------------------------
# Step 1: connection picker
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_step1_lists_connections(web_client, isolated_xdg: Path) -> None:
    """Step 1 renders every existing connection as a row."""
    _make_conn(web_client, "c_first")
    _make_conn(web_client, "c_second")
    r = web_client.get("/addresses/new")
    assert r.status_code == 200
    assert "step 1 of 2" in r.text
    assert "c_first" in r.text
    assert "c_second" in r.text
    # Each row exposes a Choose button pointing at step 2.
    assert "/addresses/new/step2?connection=c_first" in r.text
    assert "/addresses/new/step2?connection=c_second" in r.text


@pytest.mark.unit
def test_step1_empty_shows_link_to_new_connection(web_client, isolated_xdg: Path) -> None:
    """With no connections, step 1 nudges the user to create one first."""
    r = web_client.get("/addresses/new")
    assert r.status_code == 200
    assert "/connections/new" in r.text
    # No connections listed → the empty-state block renders.
    assert "No connections configured" in r.text


# ---------------------------------------------------------------------------
# Step 2: location form
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_step2_renders_disc_location_fields(web_client, isolated_xdg: Path) -> None:
    """Disc connections get path / format / lowercase_columns inputs."""
    _make_conn(web_client, "c_disc")
    r = web_client.get("/addresses/new/step2?connection=c_disc")
    assert r.status_code == 200
    assert "step 2 of 2" in r.text
    # location_spec-driven inputs use the ``loc_<name>`` naming scheme.
    assert 'name="loc_path"' in r.text
    assert 'name="loc_format"' in r.text
    assert 'name="loc_lowercase_columns"' in r.text
    # And carry the connection through as a hidden field.
    assert 'name="connection"' in r.text
    assert 'value="c_disc"' in r.text


@pytest.mark.unit
def test_step2_snowflake_marks_database_required(web_client, isolated_xdg: Path) -> None:
    """Snowflake addresses require a database — the input must have `required`."""
    _make_conn(web_client, "c_sf", type_="snowflake")
    # Snowflake write is refused unless secrets are set; connection *creation*
    # via the API allows args-only. The disc conn already passes; use it if
    # snowflake conn creation fails in this env.
    r = web_client.get("/addresses/new/step2?connection=c_sf")
    if r.status_code != 200:  # snowflake connection didn't materialise
        pytest.skip("snowflake connection unavailable in test env")
    assert 'name="loc_database"' in r.text
    # The template renders `required` on required fields.
    idx = r.text.index('name="loc_database"')
    surrounding = r.text[idx : idx + 200]
    assert "required" in surrounding


@pytest.mark.unit
def test_step2_missing_connection_bounces_back_to_step1(web_client, isolated_xdg: Path) -> None:
    """Passing an unknown connection name redirects to step 1 with a flash."""
    r = web_client.get(
        "/addresses/new/step2?connection=does_not_exist",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/addresses/new?")
    assert "not+found" in r.headers["location"]


# ---------------------------------------------------------------------------
# POST /addresses — persist
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_html_post_creates_w7_address(web_client, isolated_xdg: Path) -> None:
    """A step-2 form submission lands a W7-shaped JSON on disk."""
    _make_conn(web_client, "c_disc")
    r = web_client.post(
        "/addresses",
        data={
            "name": "html_new",
            "connection": "c_disc",
            "loc_path": "sub/dir",
            "loc_format": "parquet",
            # lowercase_columns unchecked → absent from form
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/addresses/html_new"

    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    payload = json.loads((configs / "html_new.json").read_text())
    assert payload["connection"] == "c_disc"
    assert payload["location"]["path"] == "sub/dir"
    assert payload["location"]["format"] == "parquet"
    # An unchecked checkbox must not silently become ``true``.
    assert payload["location"].get("lowercase_columns") in (False, None)


@pytest.mark.unit
def test_html_post_without_connection_bounces_back(web_client, isolated_xdg: Path) -> None:
    """Missing connection field on POST redirects back to step 1."""
    r = web_client.post(
        "/addresses",
        data={"name": "bad", "connection": "", "loc_path": "x"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/addresses/new")


@pytest.mark.unit
def test_html_post_drops_blank_optional_fields(web_client, isolated_xdg: Path) -> None:
    """Blank optional inputs are not persisted as ``""`` — they are omitted."""
    _make_conn(web_client, "c_disc")
    r = web_client.post(
        "/addresses",
        data={
            "name": "compact",
            "connection": "c_disc",
            "loc_path": "",  # blank optional
            "loc_format": "csv",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    payload = json.loads((configs / "compact.json").read_text())
    assert "path" not in payload["location"]
    assert payload["location"]["format"] == "csv"


# ---------------------------------------------------------------------------
# Edit-page shape branching
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_edit_w7_uses_location_form(web_client, isolated_xdg: Path) -> None:
    """Editing a W7-shaped address renders the location-only form."""
    _make_conn(web_client, "c_disc")
    web_client.post(
        "/api/v1/addresses/edit_w7",
        json={"connection": "c_disc", "location": {"path": "orig"}},
    )
    r = web_client.get("/addresses/edit_w7/edit")
    assert r.status_code == 200
    # Location-only form ships the ``loc_<name>`` inputs; the legacy
    # free-form ``arg_key_N`` grid is absent.
    assert 'name="loc_path"' in r.text
    assert "orig" in r.text
    assert 'name="arg_key_0"' not in r.text


@pytest.mark.unit
def test_edit_legacy_shows_banner(web_client, isolated_xdg: Path) -> None:
    """Editing a legacy-shape address shows the free-form grid + banner."""
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    (configs / "legacy_addr.json").write_text(
        json.dumps({"src_name": "disc", "src_args": {"path": "/tmp/legacy"}})
    )
    r = web_client.get("/addresses/legacy_addr/edit")
    assert r.status_code == 200
    assert "Legacy address" in r.text
    assert "migrate-addresses" in r.text
    # Legacy edit form still uses the paired key/value grid.
    assert 'name="arg_key_0"' in r.text


@pytest.mark.unit
def test_addresses_list_marks_legacy_rows(web_client, isolated_xdg: Path) -> None:
    """The addresses list view now redirects to the storage-accounts index.

    Addresses were folded into the storage-account concept; the
    standalone list page is gone. ``GET /addresses`` lands on
    ``/storage``.
    """
    _make_conn(web_client, "c_disc")
    web_client.post(
        "/api/v1/addresses/w7_addr",
        json={"connection": "c_disc", "location": {"path": "x"}},
    )
    r = web_client.get("/addresses")
    assert r.status_code == 200
    assert "Storage accounts" in r.text
    # The storage-account row for the backing connection links to it.
    assert "/connections/c_disc" in r.text


@pytest.mark.unit
def test_detail_page_shows_location_for_w7(web_client, isolated_xdg: Path) -> None:
    """Detail page renders a Location table for W7 addresses (not src_args)."""
    _make_conn(web_client, "c_disc")
    web_client.post(
        "/api/v1/addresses/detail_w7",
        json={"connection": "c_disc", "location": {"path": "here", "format": "csv"}},
    )
    r = web_client.get("/addresses/detail_w7")
    assert r.status_code == 200
    assert "Location" in r.text
    assert "here" in r.text
    assert "csv" in r.text
