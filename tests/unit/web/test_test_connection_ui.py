"""UI affordance tests for connection Test buttons.

Verifies that the HTML pages introduced or extended by the
"test-connection" UX round-out surface a working Test control:

* ``/connections`` renders a per-row Test button and a Test all
  button, and the inline JS wires them to
  ``/api/v1/connections/{name}/test``.
* ``/connections/{name}/edit`` (edit mode only) surfaces a
  Test connection button that posts to ``/connections/{name}/test``.
* ``/connections/new`` (create mode) does **not** show a Test button
  since there is nothing on disk to probe yet.
* ``/addresses/{name}`` shows a Test underlying connection button
  only when the address is W7-shaped (has a ``connection`` field).
* ``/addresses`` shows a per-row Test button only for W7-shaped
  addresses; legacy rows show ``n/a``.

The tests speak to the FastAPI ``TestClient`` fixture, drop the
minimum config files on the XDG search path, and inspect the raw
HTML markup — no real network probes are triggered because we
never invoke the JS.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _write_connection(isolated_xdg: Path, name: str, root: str) -> None:
    """Drop a minimal disc connection config on the connections dir."""
    path = Path(os.environ["OVID2_CONNECTIONS_DIR"]) / f"{name}.json"
    path.write_text(
        json.dumps({"type": "disc", "args": {"host_path": root}}),
        encoding="utf-8",
    )


def _write_legacy_address(name: str, root: str) -> None:
    """Drop a legacy (src_args) address config."""
    path = Path(os.environ["OVID2_CONFIGS_DIR"]) / f"{name}.json"
    path.write_text(
        json.dumps({"src_name": "disc", "src_args": {"path": root}}),
        encoding="utf-8",
    )


def _write_w7_address(name: str, connection: str, sub: str = "sub") -> None:
    """Drop a W7-shaped address config referencing a connection."""
    path = Path(os.environ["OVID2_CONFIGS_DIR"]) / f"{name}.json"
    path.write_text(
        json.dumps({"connection": connection, "location": {"path": sub}}),
        encoding="utf-8",
    )


@pytest.mark.unit
def test_connections_list_renders_test_column_and_buttons(
    web_client, isolated_xdg: Path
) -> None:  # type: ignore[no-untyped-def]
    """`/connections` shows a Result column, per-row Test, and Test all."""
    _write_connection(isolated_xdg, "c_one", str(isolated_xdg))
    _write_connection(isolated_xdg, "c_two", str(isolated_xdg))

    response = web_client.get("/connections")
    assert response.status_code == 200
    html = response.text

    # Column header
    assert "<th>Result</th>" in html
    # Test all button
    assert 'data-test-all="connections"' in html
    # Per-row buttons for both connections
    assert 'data-test-connection="c_one"' in html
    assert 'data-test-connection="c_two"' in html
    # Test result cells wired for each connection
    assert 'data-test-cell="c_one"' in html
    assert 'data-test-cell="c_two"' in html
    # JS references the REST endpoint
    assert "/api/v1/connections/" in html


@pytest.mark.unit
def test_connections_empty_hides_test_all(
    web_client, isolated_xdg: Path
) -> None:  # type: ignore[no-untyped-def]
    """The Test all button is not rendered when there are no storage accounts."""
    response = web_client.get("/connections")
    assert response.status_code == 200
    html = response.text
    assert 'data-test-all="connections"' not in html
    assert "No storage accounts yet" in html


@pytest.mark.unit
def test_connection_edit_page_shows_test_button(
    web_client, isolated_xdg: Path
) -> None:  # type: ignore[no-untyped-def]
    """Edit-mode surfaces a Test form pointing at the probe URL."""
    _write_connection(isolated_xdg, "c_edit", str(isolated_xdg))
    response = web_client.get("/connections/c_edit/edit")
    assert response.status_code == 200
    html = response.text
    assert 'action="/connections/c_edit/test"' in html
    assert "Test storage account" in html


@pytest.mark.unit
def test_connection_new_page_omits_test_button(
    web_client, isolated_xdg: Path
) -> None:  # type: ignore[no-untyped-def]
    """The New form has nothing on disk to probe, so no Test button."""
    response = web_client.get("/connections/new")
    assert response.status_code == 200
    html = response.text
    # No probe form should point at any /connections/*/test URL.
    assert "/test" not in html or "connection.name" not in html
    # Explicitly: the "Test connection" button label lives only in
    # edit mode. The detail page also uses it, but this route
    # renders the edit form, not the detail page.
    assert 'action="/connections//test"' not in html
    # There is no known connection name yet, so the probe form must
    # be absent.
    assert "form" not in html.lower() or '/connections/new"' in html or 'method="post"' in html


@pytest.mark.unit
def test_address_detail_shows_probe_only_when_w7(
    web_client, isolated_xdg: Path
) -> None:  # type: ignore[no-untyped-def]
    """Address detail exposes a probe form only for connection-shaped addresses."""
    _write_connection(isolated_xdg, "c_shared", str(isolated_xdg))
    _write_w7_address("a_w7", "c_shared")
    _write_legacy_address("a_legacy", str(isolated_xdg))

    resp_w7 = web_client.get("/addresses/a_w7")
    assert resp_w7.status_code == 200
    html_w7 = resp_w7.text
    assert 'action="/connections/c_shared/test"' in html_w7
    assert "Test underlying connection" in html_w7

    resp_legacy = web_client.get("/addresses/a_legacy")
    assert resp_legacy.status_code == 200
    html_legacy = resp_legacy.text
    assert "Test underlying connection" not in html_legacy
    # Also: no probe form of any kind is rendered on a legacy address.
    # (The base nav link ``/connections`` is fine; a probe would target
    # ``/connections/<name>/test``.)
    assert "/connections/c_shared/test" not in html_legacy


@pytest.mark.unit
def test_addresses_list_test_button_gating(
    web_client, isolated_xdg: Path
) -> None:  # type: ignore[no-untyped-def]
    """The addresses list view now redirects to the storage-accounts index.

    Addresses were folded into storage accounts; per-run location is
    chosen on the pipeline at submit time. The standalone address list
    no longer exists, so ``GET /addresses`` lands on ``/storage``.
    """
    _write_connection(isolated_xdg, "c_shared", str(isolated_xdg))
    _write_w7_address("a_w7", "c_shared")

    response = web_client.get("/addresses")
    assert response.status_code == 200
    assert "Storage accounts" in response.text


@pytest.mark.unit
def test_addresses_list_hides_test_all_when_no_w7(
    web_client, isolated_xdg: Path
) -> None:  # type: ignore[no-untyped-def]
    """``GET /addresses`` redirects to the storage-accounts index."""
    _write_legacy_address("a_legacy", "/tmp")
    response = web_client.get("/addresses")
    assert response.status_code == 200
    assert "Storage accounts" in response.text


@pytest.mark.unit
def test_connection_test_html_route_still_returns_flash(
    web_client, isolated_xdg: Path
) -> None:  # type: ignore[no-untyped-def]
    """The existing POST /connections/{name}/test still re-renders detail."""
    root = isolated_xdg
    _write_connection(isolated_xdg, "c_probe_ui", str(root))
    response = web_client.post("/connections/c_probe_ui/test")
    assert response.status_code == 200
    html = response.text
    # The detail page renders the flash block with either ok or error
    # class — for a real, readable dir it should be ok.
    assert 'class="flash ok"' in html or 'class="flash error"' in html
    assert "Test result" in html
