"""Unit tests for the DuckDB -> Snowflake write path.

Regression (ACME ``encounter``, 2026-11-04): the plan was written to one
Parquet file and ``PUT`` + ``COPY INTO`` loaded it on a single thread —
5 minutes of a 5.5-minute table. The write must produce a directory of
parts and upload them with one globbed, parallel ``PUT``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ovid2.core.engines.duckdb import DuckDBEngine
from ovid2.drivers.snowflake import duckdb as sf_duckdb


class _Cursor:
    def __init__(self, log: list[str], seen: dict[str, Any]) -> None:
        self._log = log
        self._seen = seen

    def execute(self, sql: str) -> None:
        self._log.append(sql)
        if sql.startswith("PUT "):
            pattern = sql.split("'", 2)[1].removeprefix("file://")
            directory = os.path.dirname(pattern)
            self._seen["parts"] = sorted(os.listdir(directory))


class _Conn:
    def __init__(self, log: list[str], seen: dict[str, Any]) -> None:
        self._log = log
        self._seen = seen

    def cursor(self) -> _Cursor:
        return _Cursor(self._log, self._seen)

    def close(self) -> None:
        pass


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[DuckDBEngine]:
    eng = DuckDBEngine(args={"threads": 4, "temp_directory": str(tmp_path / "duck")})
    yield eng
    eng.stop_session()


def _persist(
    engine: DuckDBEngine, monkeypatch: pytest.MonkeyPatch, df: Any
) -> tuple[list[str], dict[str, Any]]:
    log: list[str] = []
    seen: dict[str, Any] = {}
    monkeypatch.setattr(sf_duckdb, "_connect", lambda *a, **kw: _Conn(log, seen))
    monkeypatch.setattr(sf_duckdb, "_PART_SIZE", "1MB")
    driver = SimpleNamespace(
        location={"database": "DB", "schema": "SCH", "cdm": "pcornet_7_0"}, args={}, secrets={}
    )
    monkeypatch.setattr(sf_duckdb, "_db_schema", lambda d, write: ("DB", "SCH"))
    strategy = sf_duckdb.DuckDBSnowflakeStrategy(engine)
    strategy.persist(df, driver, "p", "b", "encounter")  # type: ignore[arg-type]
    return log, seen


@pytest.mark.unit
def test_persist_uploads_many_parts_with_one_parallel_put(
    engine: DuckDBEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = engine.connection()
    rel = conn.sql(
        "SELECT md5(i::VARCHAR) AS ENCOUNTERID, md5((i * 7)::VARCHAR) AS PATID "
        "FROM range(400000) r(i)"
    )
    log, seen = _persist(engine, monkeypatch, engine.wrap(rel))

    puts = [s for s in log if s.startswith("PUT ")]
    assert len(puts) == 1
    assert "/*.parquet'" in puts[0]
    assert "PARALLEL = 4 " in puts[0]
    assert len(seen["parts"]) > 1, seen
    assert sum(1 for s in log if s.startswith("COPY INTO")) == 1
    assert engine.rows_written_total == 400000


@pytest.mark.unit
def test_persist_empty_frame_creates_table_without_copy(
    engine: DuckDBEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = engine.connection().sql("SELECT 'x' AS ENCOUNTERID WHERE false")
    log, _seen = _persist(engine, monkeypatch, engine.wrap(rel))
    assert any(s.startswith("CREATE OR REPLACE TABLE") for s in log)
    assert engine.rows_written_total == 0
