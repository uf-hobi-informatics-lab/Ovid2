"""Unit tests for Snowflake storage browse (``list_dir`` — DB→schema→table).

Uses a stubbed ``snowflake.connector`` module so the tests run without
the real SDK or a live account, mirroring the pattern in
``test_probe.py``.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from ovid2.drivers.snowflake import probe
from ovid2.drivers.snowflake.driver import SnowflakeDriver


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self.conn = conn
        self.description: list[tuple[str]] | None = None
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, stmt: str) -> None:
        self.conn.statements.append(stmt)
        # ``SHOW`` results carry ``created_on`` first, ``name`` second.
        self.description = [("created_on",), ("name",)]
        if stmt == "SHOW DATABASES":
            self._rows = [("x", "ANALYTICS"), ("x", "SNOWFLAKE"), ("x", "RAW")]
        elif stmt.startswith("SHOW SCHEMAS"):
            self._rows = [("x", "PUBLIC"), ("x", "INFORMATION_SCHEMA"), ("x", "OVID")]
        elif stmt.startswith("SHOW TABLES"):
            self._rows = [("x", "DEMOGRAPHIC"), ("x", "ENCOUNTER")]
        elif stmt.startswith("SHOW VIEWS"):
            self._rows = [("x", "V_SUMMARY")]
        else:
            self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def close(self) -> None:  # pragma: no cover - trivial
        pass


class _FakeConn:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def fake_connector(monkeypatch: pytest.MonkeyPatch) -> list[_FakeConn]:
    """Install a stub ``snowflake.connector`` and capture connections."""
    conns: list[_FakeConn] = []
    fake_sf = types.ModuleType("snowflake")
    fake_conn_mod = types.ModuleType("snowflake.connector")

    def _connect(**_kwargs: Any) -> _FakeConn:
        c = _FakeConn()
        conns.append(c)
        return c

    fake_conn_mod.connect = _connect  # type: ignore[attr-defined]
    fake_sf.connector = fake_conn_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "snowflake", fake_sf)
    monkeypatch.setitem(sys.modules, "snowflake.connector", fake_conn_mod)
    return conns


ARGS = {"account": "a", "user": "u"}
SECRETS = {"password": "p"}  # password auth avoids key-file reads


@pytest.mark.unit
def test_quote_identifier_ok() -> None:
    assert probe._quote_identifier("MY_DB") == '"MY_DB"'


@pytest.mark.unit
def test_quote_identifier_rejects_injection() -> None:
    with pytest.raises(ValueError):
        probe._quote_identifier("bad; DROP TABLE x")


@pytest.mark.unit
def test_list_databases_filters_system(fake_connector: list[_FakeConn]) -> None:
    entries = probe.list_dir_from_config(ARGS, SECRETS, None)
    names = [(e.name, e.kind, e.path) for e in entries]
    assert ("ANALYTICS", "dir", "ANALYTICS") in names
    assert ("RAW", "dir", "RAW") in names
    # SNOWFLAKE system database hidden.
    assert all(e.name != "SNOWFLAKE" for e in entries)
    assert fake_connector[0].closed is True


@pytest.mark.unit
def test_list_schemas(fake_connector: list[_FakeConn]) -> None:
    entries = probe.list_dir_from_config(ARGS, SECRETS, "ANALYTICS")
    paths = {e.path for e in entries}
    assert "ANALYTICS/OVID" in paths
    assert "ANALYTICS/PUBLIC" in paths
    # INFORMATION_SCHEMA hidden.
    assert all(e.name != "INFORMATION_SCHEMA" for e in entries)
    # Statement was scoped to the database.
    assert any("SHOW SCHEMAS IN DATABASE" in s for s in fake_connector[0].statements)


@pytest.mark.unit
def test_list_tables_merges_tables_and_views(
    fake_connector: list[_FakeConn],
) -> None:
    entries = probe.list_dir_from_config(ARGS, SECRETS, "ANALYTICS/OVID")
    names = sorted(e.name for e in entries)
    assert names == ["DEMOGRAPHIC", "ENCOUNTER", "V_SUMMARY"]
    assert all(e.kind == "table" for e in entries)
    assert all(e.path.startswith("ANALYTICS/OVID/") for e in entries)


@pytest.mark.unit
def test_prefix_too_deep_raises(fake_connector: list[_FakeConn]) -> None:
    with pytest.raises(ValueError):
        probe.list_dir_from_config(ARGS, SECRETS, "a/b/c")


@pytest.mark.unit
def test_driver_list_dir_delegates(fake_connector: list[_FakeConn]) -> None:
    driver = SnowflakeDriver(args=ARGS, secrets=SECRETS, location={})
    entries = driver.list_dir(None)
    assert any(e.name == "ANALYTICS" for e in entries)
