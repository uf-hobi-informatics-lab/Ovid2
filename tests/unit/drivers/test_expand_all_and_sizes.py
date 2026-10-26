"""Unit tests for ``all`` expansion + size hints on folder / schema drivers.

Covers:

- :mod:`ovid2.drivers._table_listing` (junk filtering, extension
  stripping, ``LOGICAL=SOURCE`` specs, glob size matching).
- :class:`~ovid2.drivers.disc.driver.DiscDriver` batch-folder
  resolution (same layout precedence as ``resolve_read_path``).
- :class:`~ovid2.drivers.snowflake.driver.SnowflakeDriver` via a stub
  ``snowflake.connector`` — ``SHOW TABLES`` ``bytes`` → sizes.
- :class:`~ovid2.drivers.s3.driver.S3Driver` /
  :class:`~ovid2.drivers.azure_blob.driver.AzureBlobDriver` via stubbed
  listings (size aggregation of ``<table>.csv/part-*`` prefixes).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from ovid2.drivers import StorageEntry
from ovid2.drivers._table_listing import (
    entries_to_table_specs,
    is_listing_junk,
    sizes_for,
    strip_table_extension,
)
from ovid2.drivers.disc.driver import DiscDriver
from ovid2.drivers.snowflake.driver import SnowflakeDriver

# ---------------------------------------------------------------------------
# _table_listing helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_storage_entry_to_dict_includes_size_only_when_known() -> None:
    assert StorageEntry("a", "file", "a").to_dict() == {"name": "a", "kind": "file", "path": "a"}
    assert StorageEntry("a", "file", "a", size_bytes=7).to_dict()["size_bytes"] == 7


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "stem"),
    [
        ("demographic.csv", "demographic"),
        ("DEMOGRAPHIC.CSV", "DEMOGRAPHIC"),
        ("lab.csv.gz", "lab"),
        ("x.snappy.parquet", "x"),
        ("noext", "noext"),
        (".csv", ".csv"),
    ],
)
def test_strip_table_extension(name: str, stem: str) -> None:
    assert strip_table_extension(name) == stem


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "junk"),
    [
        ("_SUCCESS", True),
        (".hidden", True),
        ("part.crc", True),
        ("year=2026", True),
        ("demographic.csv", False),
    ],
)
def test_is_listing_junk(name: str, junk: bool) -> None:
    assert is_listing_junk(name) is junk


@pytest.mark.unit
def test_entries_to_table_specs_filters_and_splits() -> None:
    entries = [
        StorageEntry("_SUCCESS", "file", "_SUCCESS"),
        StorageEntry("demographic.csv", "file", "d"),
        StorageEntry("encounter.csv", "dir", "e"),  # Spark-written dir
        StorageEntry("logs", "dir", "logs"),  # plain sub-folder: skipped
        StorageEntry("HARVEST", "table", "h"),
        StorageEntry("demographic.parquet", "file", "dp"),  # dup stem
    ]
    assert entries_to_table_specs(entries) == [
        "demographic=demographic.csv",
        "encounter=encounter.csv",
        "HARVEST",
    ]


@pytest.mark.unit
def test_sizes_for_exact_glob_and_unknown() -> None:
    entries = [
        StorageEntry("a.csv", "file", "a", size_bytes=10),
        StorageEntry("b_part1.csv", "file", "b1", size_bytes=5),
        StorageEntry("b_part2.csv", "file", "b2", size_bytes=7),
        StorageEntry("c.csv", "file", "c"),  # unknown size
    ]
    assert sizes_for(entries, ["A.csv", "*b_part*", "c.csv", "zzz"]) == {
        "A.csv": 10,
        "*b_part*": 12,
    }


# ---------------------------------------------------------------------------
# Disc
# ---------------------------------------------------------------------------


def _disc(root: Path, sub: str = "") -> DiscDriver:
    return DiscDriver(
        args={"host_path": str(root), "container_path": "/nonexistent-ovid2", "mount_mode": "ro"},
        secrets={},
        location={"path": sub} if sub else {},
    )


@pytest.mark.unit
def test_disc_supports_expand_all() -> None:
    assert DiscDriver.supports_expand_all() is True


@pytest.mark.unit
def test_disc_expand_all_lists_address_folder_exactly(tmp_path: Path) -> None:
    """``all`` lists the address folder itself — never ``{pipeline}/{batch}``."""
    batch = tmp_path / "landing" / "20260101"
    batch.mkdir(parents=True)
    (batch / "demographic.csv").write_text("x" * 10)
    (batch / "encounter.csv").write_text("x" * 99)
    (batch / "_SUCCESS").write_text("")
    specs = _disc(tmp_path, "landing/20260101").expand_all_tables(
        pipeline="demo", batch_id="landing/20260101"
    )
    assert specs == ["demographic=demographic.csv", "encounter=encounter.csv"]


@pytest.mark.unit
def test_disc_expand_all_ignores_pipeline_and_batch(tmp_path: Path) -> None:
    """Pipeline / batch never add path segments; sub-folders are not tables."""
    (tmp_path / "death.csv").write_text("x")
    (tmp_path / "demo" / "20260101").mkdir(parents=True)
    (tmp_path / "demo" / "20260101" / "vital.csv").write_text("x")
    assert _disc(tmp_path).expand_all_tables(pipeline="demo", batch_id="20260101") == [
        "death=death.csv"
    ]
    assert _disc(tmp_path).expand_all_tables() == ["death=death.csv"]


@pytest.mark.unit
def test_disc_expand_all_missing_root_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _disc(tmp_path / "missing").expand_all_tables(pipeline="p", batch_id="b")


@pytest.mark.unit
def test_disc_table_sizes_files_dirs_and_globs(tmp_path: Path) -> None:
    batch = tmp_path / "p" / "b"
    batch.mkdir(parents=True)
    (batch / "small.csv").write_text("x" * 3)
    spark_dir = batch / "big.csv"
    spark_dir.mkdir()
    (spark_dir / "part-0.csv").write_text("x" * 50)
    (spark_dir / "part-1.csv").write_text("x" * 50)
    (batch / "x_datavant_person_tokens_1.csv").write_text("x" * 20)
    sizes = _disc(tmp_path, "p/b").table_sizes(
        ["small.csv", "big.csv", "*datavant_person_tokens*", "absent.csv"],
        pipeline="p",
        batch_id="b",
    )
    assert sizes == {"small.csv": 3, "big.csv": 100, "*datavant_person_tokens*": 20}


@pytest.mark.unit
def test_disc_list_dir_reports_file_sizes(tmp_path: Path) -> None:
    (tmp_path / "a.csv").write_text("x" * 4)
    (tmp_path / "sub").mkdir()
    entries = {e.name: e for e in _disc(tmp_path).list_dir()}
    assert entries["a.csv"].size_bytes == 4
    assert entries["sub"].size_bytes is None


# ---------------------------------------------------------------------------
# Snowflake
# ---------------------------------------------------------------------------


class _SfCursor:
    def __init__(self, conn: _SfConn) -> None:
        self.conn = conn
        self.description: list[tuple[str]] | None = None
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, stmt: str) -> None:
        self.conn.statements.append(stmt)
        if stmt.startswith("SHOW TABLES"):
            self.description = [("created_on",), ("name",), ("rows",), ("bytes",)]
            self._rows = [
                ("x", "DEMOGRAPHIC", 10, 1_000),
                ("x", "LAB_RESULT_CM", 99, 9_000_000),
                ("x", "HARVEST", 1, None),
            ]
        elif stmt.startswith("SHOW VIEWS"):
            self.description = [("created_on",), ("name",)]
            self._rows = [("x", "V_SUMMARY")]
        else:
            self.description, self._rows = [], []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def close(self) -> None:
        pass


class _SfConn:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def cursor(self) -> _SfCursor:
        return _SfCursor(self)

    def close(self) -> None:
        pass


@pytest.fixture()
def sf_conns(monkeypatch: pytest.MonkeyPatch) -> list[_SfConn]:
    conns: list[_SfConn] = []
    fake_sf = types.ModuleType("snowflake")
    fake_conn_mod = types.ModuleType("snowflake.connector")

    def _connect(**_kwargs: Any) -> _SfConn:
        c = _SfConn()
        conns.append(c)
        return c

    fake_conn_mod.connect = _connect  # type: ignore[attr-defined]
    fake_sf.connector = fake_conn_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "snowflake", fake_sf)
    monkeypatch.setitem(sys.modules, "snowflake.connector", fake_conn_mod)
    return conns


def _sf(location: dict[str, Any]) -> SnowflakeDriver:
    return SnowflakeDriver(
        args={"account": "a", "user": "u"}, secrets={"password": "p"}, location=location
    )


@pytest.mark.unit
def test_snowflake_expand_all_uses_path_database_schema(sf_conns: list[_SfConn]) -> None:
    assert SnowflakeDriver.supports_expand_all() is True
    tables = _sf({"path": "CHAR_DEMO/PUBLIC"}).expand_all_tables(pipeline="p", batch_id="b")
    assert tables == ["DEMOGRAPHIC", "HARVEST", "LAB_RESULT_CM", "V_SUMMARY"]
    assert sf_conns[0].statements == [
        'SHOW TABLES IN SCHEMA "CHAR_DEMO"."PUBLIC"',
        'SHOW VIEWS IN SCHEMA "CHAR_DEMO"."PUBLIC"',
    ]


@pytest.mark.unit
def test_snowflake_explicit_keys_win_and_schema_defaults(sf_conns: list[_SfConn]) -> None:
    _sf({"path": "IGNORED/X", "database": "DB", "schema": "S"}).expand_all_tables()
    _sf({"path": "DB2"}).expand_all_tables()
    assert sf_conns[0].statements[0] == 'SHOW TABLES IN SCHEMA "DB"."S"'
    assert sf_conns[1].statements[0] == 'SHOW TABLES IN SCHEMA "DB2"."PUBLIC"'


@pytest.mark.unit
def test_snowflake_table_sizes_case_insensitive(sf_conns: list[_SfConn]) -> None:
    sizes = _sf({"path": "DB/S"}).table_sizes(
        ["demographic", "lab_result_cm", "harvest", "v_summary", "nope"],
        pipeline="p",
        batch_id="b",
    )
    assert sizes == {"demographic": 1_000, "lab_result_cm": 9_000_000}


@pytest.mark.unit
def test_snowflake_no_database_raises() -> None:
    with pytest.raises(ValueError, match="no database"):
        _sf({}).expand_all_tables()


@pytest.mark.unit
def test_snowflake_list_dir_tables_carry_bytes(sf_conns: list[_SfConn]) -> None:
    entries = {e.name: e for e in _sf({}).list_dir("DB/S")}
    assert entries["LAB_RESULT_CM"].size_bytes == 9_000_000
    assert entries["V_SUMMARY"].size_bytes is None


# ---------------------------------------------------------------------------
# S3 / Azure Blob (size aggregation under the batch prefix)
# ---------------------------------------------------------------------------


class _FakePaginator:
    def __init__(self, keys: dict[str, int]) -> None:
        self.keys = keys
        self.calls: list[dict[str, Any]] = []

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        prefix = kwargs.get("Prefix", "")
        return [
            {
                "Contents": [
                    {"Key": k, "Size": v} for k, v in self.keys.items() if k.startswith(prefix)
                ]
            }
        ]


@pytest.mark.unit
def test_s3_expand_all_and_sizes(monkeypatch: pytest.MonkeyPatch) -> None:
    from ovid2.drivers.s3 import probe as s3_probe
    from ovid2.drivers.s3.driver import S3Driver

    paginator = _FakePaginator(
        {
            "root/demographic.csv": 10,
            "root/lab_result_cm.csv/part-0.csv": 500,
            "root/lab_result_cm.csv/part-1.csv": 500,
            "root/_SUCCESS": 0,
        }
    )

    class _Client:
        def get_paginator(self, _name: str) -> _FakePaginator:
            return paginator

    monkeypatch.setitem(sys.modules, "boto3", types.ModuleType("boto3"))
    monkeypatch.setattr(s3_probe, "_build_s3_client", lambda *_a: _Client())
    driver = S3Driver(args={"bucket_default": "bkt"}, secrets={}, location={"path": "root"})
    assert S3Driver.supports_expand_all() is True
    assert driver.expand_all_tables(pipeline="demo", batch_id="20260101") == [
        "demographic=demographic.csv",
        "lab_result_cm=lab_result_cm.csv",
    ]
    assert paginator.calls[0] == {"Bucket": "bkt", "Prefix": "root/"}
    sizes = driver.table_sizes(
        ["demographic.csv", "lab_result_cm.csv"], pipeline="demo", batch_id="20260101"
    )
    assert sizes == {"demographic.csv": 10, "lab_result_cm.csv": 1000}


@pytest.mark.unit
def test_s3_expand_all_empty_prefix_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from ovid2.drivers.s3 import probe as s3_probe
    from ovid2.drivers.s3.driver import S3Driver

    class _Client:
        def get_paginator(self, _name: str) -> _FakePaginator:
            return _FakePaginator({})

    monkeypatch.setitem(sys.modules, "boto3", types.ModuleType("boto3"))
    monkeypatch.setattr(s3_probe, "_build_s3_client", lambda *_a: _Client())
    driver = S3Driver(args={"bucket_default": "bkt"}, secrets={}, location={})
    with pytest.raises(FileNotFoundError):
        driver.expand_all_tables(pipeline="demo", batch_id="b")


class _FakeBlob:
    def __init__(self, name: str, size: int) -> None:
        self.name = name
        self.size = size


@pytest.mark.unit
def test_azure_blob_expand_all_and_sizes(monkeypatch: pytest.MonkeyPatch) -> None:
    from ovid2.drivers.azure_blob import probe as az_probe
    from ovid2.drivers.azure_blob.driver import AzureBlobDriver

    blobs = [
        _FakeBlob("SANDBOX/b1/encounter.csv", 40),
        _FakeBlob("SANDBOX/b1/vital.csv/part-0", 7),
        _FakeBlob("SANDBOX/b1/vital.csv/part-1", 8),
    ]
    seen: list[str | None] = []

    class _Container:
        def list_blobs(self, name_starts_with: str | None = None) -> list[_FakeBlob]:
            seen.append(name_starts_with)
            return [b for b in blobs if b.name.startswith(name_starts_with or "")]

    class _Service:
        def get_container_client(self, _name: str) -> _Container:
            return _Container()

    azure_pkg = types.ModuleType("azure")
    storage_pkg = types.ModuleType("azure.storage")
    blob_pkg = types.ModuleType("azure.storage.blob")
    monkeypatch.setitem(sys.modules, "azure", azure_pkg)
    monkeypatch.setitem(sys.modules, "azure.storage", storage_pkg)
    monkeypatch.setitem(sys.modules, "azure.storage.blob", blob_pkg)
    monkeypatch.setattr(az_probe, "_build_client", lambda *_a: _Service())
    driver = AzureBlobDriver(
        args={"account_name": "acct", "container_default": "data"},
        secrets={"account_key": "k"},
        location={"path": "SANDBOX/b1"},
    )
    assert driver.expand_all_tables(pipeline="demo", batch_id="SANDBOX/b1") == [
        "encounter=encounter.csv",
        "vital=vital.csv",
    ]
    assert seen[0] == "SANDBOX/b1/"
    assert driver.table_sizes(["vital.csv"], pipeline="demo", batch_id="b1") == {"vital.csv": 15}
