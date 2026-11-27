"""Native DuckDB legacy text I/O is byte-identical to the pure-Python path.

``ovid2.legacy.text_io`` (``write_frame_file`` / ``write_text_file`` /
``read_tab_file``) and ``csv_roundtrip._portable_write_normalise`` take a
native DuckDB SQL path on the DuckDB engine. These tests force the
original Python implementation as the oracle and require identical bytes
(writers, as a row multiset — row order was never part of legacy parity)
and identical rows (reader / write-normalise) over fuzzed values covering
every rule: NUL, tab, CR/LF, ``#`` at line start, backslashes, quotes,
whitespace-only, NULL, and typed columns (int, bool, decimal, date,
timestamp, double).
"""

from __future__ import annotations

import os
import random
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("duckdb")

from ovid2.core.engines import dsl
from ovid2.core.engines.duckdb import DuckDBEngine
from ovid2.legacy import csv_roundtrip, text_io

pytestmark = pytest.mark.unit

_ALPHA = ["a", " ", "\t", "\x00", "\\", "#", "\n", "\r", '"', "\x1f", "é", "\x01", "x"]


@pytest.fixture()
def engine() -> Iterator[DuckDBEngine]:
    eng = DuckDBEngine(args={"threads": 4, "memory_limit": "2GB"})
    dsl.set_active_engine(eng)
    yield eng
    eng.stop_session()
    dsl.set_active_engine(None)


def _strings(n: int, seed: int) -> list[str | None]:
    rnd = random.Random(seed)
    out: list[str | None] = [None, "", " ", "#x", "a\tb"]
    out += ["".join(rnd.choice(_ALPHA) for _ in range(rnd.randint(0, 8))) for _ in range(n)]
    return out


def _typed_frame(engine: DuckDBEngine, n: int = 3000) -> Any:
    conn = engine.connection()
    vals = _strings(n, seed=11)
    conn.execute(
        "CREATE OR REPLACE TABLE __t (s VARCHAR, h VARCHAR, i BIGINT, b BOOLEAN, "
        "d DECIMAL(9,2), dt DATE, ts TIMESTAMP, f DOUBLE)"
    )
    rnd = random.Random(3)
    rows = []
    for k, v in enumerate(vals):
        rows.append(
            [
                v,
                rnd.choice(["#lead", "x", None, " #", ""]),
                None if k % 7 == 0 else k - 50,
                None if k % 5 == 0 else bool(k % 2),
                None if k % 4 == 0 else (k - 1500) / 100,
                None if k % 6 == 0 else f"20{k % 30:02d}-0{1 + k % 9}-1{k % 9}",
                None if k % 9 == 0 else f"2026-01-02 03:04:{k % 60:02d}" + (".5" if k % 2 else ""),
                None if k % 8 == 0 else (k - 1000) * 1.25e-4,
            ]
        )
    conn.executemany("INSERT INTO __t VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return engine.wrap(conn.sql("SELECT * FROM __t"))


def _python_write(df: Any, folder: str, name: str, *, raw: bool, monkeypatch: Any) -> bytes:
    with monkeypatch.context() as m:
        m.setattr(text_io, "_native_engine", lambda _obj: None)
        text_io._write(df, folder, name, raw=raw)
    return Path(folder, name).read_bytes()


def _lines(data: bytes) -> tuple[bytes, list[bytes]]:
    head, _, body = data.partition(b"\n")
    return head, sorted(body.split(b"\n"))


@pytest.mark.parametrize("cols", [["s", "h"], ["s", "h", "i", "b", "d", "dt", "ts", "f"]])
def test_write_frame_file_native_equals_python(
    engine: DuckDBEngine, tmp_path: Path, monkeypatch: Any, cols: list[str]
) -> None:
    frame = _typed_frame(engine).select(*cols)
    expected = _python_write(
        frame, str(tmp_path / "py"), "t.csv", raw=False, monkeypatch=monkeypatch
    )
    text_io.write_frame_file(frame, str(tmp_path / "na"), "t.csv")
    got = Path(tmp_path / "na" / "t.csv").read_bytes()
    assert _lines(got) == _lines(expected)


def test_write_text_file_native_equals_python(
    engine: DuckDBEngine, tmp_path: Path, monkeypatch: Any
) -> None:
    frame = _typed_frame(engine).select("s", "h", "i", "d", "dt")
    expected = _python_write(
        frame, str(tmp_path / "py"), "t.csv", raw=True, monkeypatch=monkeypatch
    )
    text_io.write_text_file(frame, str(tmp_path / "na"), "t.csv")
    got = Path(tmp_path / "na" / "t.csv").read_bytes()
    assert _lines(got) == _lines(expected)


@pytest.mark.parametrize("escape", ['"', "\\"])
def test_read_tab_file_native_equals_python(
    engine: DuckDBEngine, tmp_path: Path, monkeypatch: Any, escape: str
) -> None:
    rnd = random.Random(7)
    alpha = ["a", " ", "\t", '"', "\\", "#", "x", "\x00", "é", "\r", "\n", "\r\n", ""]
    for trial in range(12):
        path = tmp_path / f"r{trial}.csv"
        header = "\t".join(
            rnd.choice(["A", "B", "", "A", '"q"', "c d"]) for _ in range(rnd.randint(1, 4))
        )
        body = ["".join(rnd.choice(alpha) for _ in range(rnd.randint(0, 10))) for _ in range(150)]
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("\n".join([header, *body]) + rnd.choice(["", "\n", "\r\n"]))
        with monkeypatch.context() as m:
            m.setattr(text_io, "_native_engine", lambda _obj: None)
            expected = text_io.read_tab_file(engine.get_session(), str(path), escape=escape)
        got = text_io.read_tab_file(engine.get_session(), str(path), escape=escape)
        assert got.columns == expected.columns
        assert sorted(got.relation.fetchall(), key=repr) == sorted(
            expected.relation.fetchall(), key=repr
        )


def test_write_normalise_native_equals_udf(engine: DuckDBEngine, monkeypatch: Any) -> None:
    frame = _typed_frame(engine)
    got = csv_roundtrip._portable_write_normalise(frame)
    with monkeypatch.context() as m:
        m.setattr(csv_roundtrip, "_native_write_normalise", lambda _df: None)
        expected = csv_roundtrip._portable_write_normalise(frame)
    assert got.columns == expected.columns
    key = repr
    assert sorted(got.relation.fetchall(), key=key) == sorted(expected.relation.fetchall(), key=key)


def test_native_writer_runs_without_python_per_cell(engine: DuckDBEngine, tmp_path: Path) -> None:
    """String-only frames never touch a Python UDF on the native path."""
    frame = _typed_frame(engine).select("s", "h", "i")
    text_io.write_frame_file(frame, str(tmp_path), "t.csv")
    assert os.path.getsize(tmp_path / "t.csv") > 0
    plan = csv_roundtrip._portable_write_normalise(frame).relation.sql_query()
    assert "__ovid2_udf" not in plan
