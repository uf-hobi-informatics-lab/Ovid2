"""Unit tests for :mod:`ovid2.drivers._layout` (address = exact location)."""

from __future__ import annotations

import pytest

from ovid2.drivers._layout import (
    BatchPathError,
    location_path,
    location_with_path,
    normalize_batch_path,
    sql_ident,
    warehouse_db_schema,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        (None, ""),
        ("landing/2026_03", "landing/2026_03"),
        ("/landing//2026_03/", "landing/2026_03"),
        (" a / b ", "a/b"),
        ("20260101", "20260101"),
    ],
)
def test_normalize_batch_path(raw: str | None, expected: str) -> None:
    assert normalize_batch_path(raw) == expected


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["a/../b", "./a", "a\\b", "a\x00b"])
def test_normalize_batch_path_rejects_escapes(raw: str) -> None:
    with pytest.raises(BatchPathError):
        normalize_batch_path(raw)


@pytest.mark.unit
def test_normalize_batch_path_warehouse_depth() -> None:
    assert normalize_batch_path("DB/SCHEMA", warehouse=True) == "DB/SCHEMA"
    with pytest.raises(BatchPathError):
        normalize_batch_path("DB/SCHEMA/TABLE", warehouse=True)


@pytest.mark.unit
def test_warehouse_db_schema_precedence() -> None:
    assert warehouse_db_schema({"path": "DB/SC"}) == ("DB", "SC")
    assert warehouse_db_schema({"path": "DB/SC", "schema": "X"}) == ("DB", "X")
    assert warehouse_db_schema({"catalog": "CAT", "schema": "S"}) == ("CAT", "S")
    assert warehouse_db_schema({"path": "DB"}) == ("DB", "")
    assert warehouse_db_schema(None) == ("", "")


@pytest.mark.unit
def test_location_path_round_trip() -> None:
    assert location_path({"path": "/a/b/"}) == "a/b"
    assert location_path({"database": "DB", "schema": "S"}, warehouse=True) == "DB/S"
    loc = location_with_path({"format": "csv", "path": "old"}, "new/leaf")
    assert loc == {"format": "csv", "path": "new/leaf"}
    wloc = location_with_path({"database": "A", "schema": "B", "cdm": "x"}, "C/D", warehouse=True)
    assert wloc == {"cdm": "x", "path": "C/D"}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("SITE_X", "SITE_X"),
        ("_a$1", "_a$1"),
        ("20260101", '"20260101"'),
        ("my-schema", '"my-schema"'),
        ('a"b', '"a""b"'),
        ('"Already"', '"Already"'),
    ],
)
def test_sql_ident(name: str, expected: str) -> None:
    assert sql_ident(name) == expected


@pytest.mark.unit
def test_sql_ident_empty_raises() -> None:
    with pytest.raises(BatchPathError):
        sql_ident("  ")
