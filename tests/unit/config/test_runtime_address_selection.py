"""Unit tests for run-time address resolution used by the reusable EDC runs.

These cover the two decoupling guarantees behind "run EDC checks against
any PCORnet database":

1. ``_resolve_snowflake_config`` takes the database/schema from the
   run-time-selected address (or ``--sf_*`` overrides) and only consults a
   per-partner package's ``DEFAULT_PIPELINE_DB_NAME`` when *no* database was
   supplied. For the generic ``pcornet_edc`` label there is no package, so
   the database always comes from the selection.
2. ``load_address_from_connection`` builds a usable address from an inline
   ``{connection, location}`` with no saved JSON config on disc.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ovid2.config import addresses as addr


@pytest.mark.unit
def test_snowflake_config_uses_selected_database(monkeypatch) -> None:
    """A supplied database is kept; the package default is never consulted."""
    import importlib

    def _boom(name: str):  # pragma: no cover - must not be called
        raise AssertionError(f"package lookup must not happen: {name}")

    monkeypatch.setattr(importlib, "import_module", _boom)
    merged = addr._resolve_snowflake_config(
        {"database": "CHAR_SITE", "schema": "PUBLIC", "account": "acct"},
        sf_overrides=None,
        pipeline="pcornet_edc",
    )
    assert merged["database"] == "CHAR_SITE"
    assert merged["schema"] == "PUBLIC"


@pytest.mark.unit
def test_snowflake_config_generic_label_no_package_fallback() -> None:
    """The generic label without a database gets no partner default DB.

    ``pcornet_edc`` has no ``ovid2_pipelines`` package, so Tier-3 lookup
    finds nothing and ``database`` stays unset — forcing the operator's
    run-time selection (address location / ``--sf_database``) to supply it.
    """
    merged = addr._resolve_snowflake_config(
        {"account": "acct"}, sf_overrides=None, pipeline="pcornet_edc"
    )
    assert "database" not in merged or not merged.get("database")


@pytest.mark.unit
def test_sf_override_wins() -> None:
    """A ``--sf_database`` override is applied last and wins."""
    merged = addr._resolve_snowflake_config(
        {"database": "FROM_ADDRESS", "account": "a"},
        sf_overrides={"database": "FROM_CLI"},
        pipeline="pcornet_edc",
    )
    assert merged["database"] == "FROM_CLI"


@pytest.mark.unit
def test_load_address_from_connection_inline_disc(
    tmp_path: Path, monkeypatch
) -> None:
    """An inline connection + location resolves without a saved address JSON."""
    conndir = tmp_path / "connections"
    conndir.mkdir()
    (conndir / "local.json").write_text(
        json.dumps(
            {
                "type": "disc",
                "args": {"host_path": "/srv/data", "container_path": "/data"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OVID2_CONNECTIONS_DIR", str(conndir))
    address = addr.load_address_from_connection("local", {"path": "char_site/2026"})
    # A concrete address object is returned (disc address here).
    assert address is not None
    assert type(address).__name__.endswith("Address")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run via pytest.")
