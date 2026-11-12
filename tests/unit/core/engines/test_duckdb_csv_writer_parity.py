"""The fast DuckDB CSV writer is byte-identical to the original renderer.

The reference below is the pre-2026-11-07 projection (per-cell
``list_transform`` wrapping of every column) plus a separately written
header — the exact bytes the parity waves were validated against.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

from ovid2.core.engines import dsl
from ovid2.core.engines._duck_column import ident
from ovid2.core.engines._java_datetime import sql_string
from ovid2.core.engines.duckdb import DuckDBEngine
from ovid2.core.engines.duckdb_stager import _shortest_double_text, write_csv_file

pytestmark = pytest.mark.unit


@pytest.fixture()
def engine() -> Iterator[DuckDBEngine]:
    eng = DuckDBEngine(args={"threads": 2, "memory_limit": "1GB"})
    dsl.set_active_engine(eng)
    yield eng
    eng.stop_session()
    dsl.set_active_engine(None)


def _reference_bytes(engine: DuckDBEngine, frame: object, delimiter: str, tmp: Path) -> bytes:
    conn = engine.connection()
    _shortest_double_text(conn)
    items = []
    for idx, (name, duck_type) in enumerate(frame.dtypes, start=1):  # type: ignore[attr-defined]
        ref, t = f"#{idx}", duck_type.upper()
        if t in ("DOUBLE", "FLOAT", "REAL"):
            text = f"__ovid2_java_double(CAST({ref} AS DOUBLE))"
        elif t == "BOOLEAN":
            text = f"CASE WHEN {ref} THEN 'true' WHEN NOT {ref} THEN 'false' END"
        elif t.startswith("TIMESTAMP"):
            text = f"strftime(CAST({ref} AS TIMESTAMP), '%Y-%m-%dT%H:%M:%S.%g') || 'Z'"
        elif t == "DATE":
            text = f"strftime({ref}, '%Y-%m-%d')"
        else:
            text = f"CAST({ref} AS VARCHAR)"
        wrap = (
            f"CASE WHEN __t IS NULL THEN '' WHEN __t = '' THEN '\"\"' "
            f"WHEN contains(__t, {sql_string(delimiter)}) OR contains(__t, chr(10)) "
            f"OR contains(__t, chr(13)) "
            f"THEN chr(0) || replace(__t, chr(0), '\\' || chr(0)) || chr(0) ELSE __t END"
        )
        items.append(f"list_transform([{text}], lambda __t: {wrap})[1] AS {ident(name)}")
    rel = frame.relation.project(", ".join(items))  # type: ignore[attr-defined]
    body = tmp / "ref_body.csv"
    rel.create_view("__ref_view", replace=True)
    conn.execute(
        f"COPY (SELECT * FROM __ref_view) TO {sql_string(str(body))} (FORMAT csv, HEADER false, "
        f"DELIMITER {sql_string(delimiter)}, QUOTE '', ESCAPE '', NULL '', PRESERVE_ORDER false)"
    )
    header = (delimiter.join(frame.columns) + "\n").encode()  # type: ignore[attr-defined]
    return header + body.read_bytes()


_ROWS = [
    ("plain", 1, 10, 1.5, 2.25, True, "2026-01-02", "2026-01-02 03:04:05.678"),
    ("", None, None, None, None, None, None, None),
    (None, -3, 2**40, 1e20, -0.0, False, "1999-12-31", "1999-12-31 23:59:59"),
    ("has\ttab", 0, 0, 0.0, 1e-5, True, None, None),
    ("has,comma", 7, 7, math.inf, math.nan, None, None, None),
    ("line\nbreak", 8, 8, -1234567.125, 3.0, None, None, None),
    ("cr\rhere", 9, 9, 123456789.0, 0.001, None, None, None),
    ("nul\x00inside\tx", 10, 10, -math.inf, 1.0, None, None, None),
]


def _typed_frame(engine: DuckDBEngine) -> object:
    conn = engine.connection()
    conn.execute(
        "CREATE OR REPLACE TABLE __w_src (S VARCHAR, I INTEGER, B BIGINT, D DOUBLE, F FLOAT, "
        "BO BOOLEAN, DT DATE, TS TIMESTAMP)"
    )
    conn.executemany("INSERT INTO __w_src VALUES (?, ?, ?, ?, ?, ?, ?, ?)", _ROWS)
    return engine.wrap(conn.table("__w_src"))


@pytest.mark.parametrize("delimiter", ["\t", ","])
def test_writer_bytes_match_reference(engine: DuckDBEngine, tmp_path: Path, delimiter: str) -> None:
    frame = _typed_frame(engine)
    target = tmp_path / "out.csv"
    rows = write_csv_file(engine, frame, str(target), delimiter=delimiter)
    assert rows == len(_ROWS)
    got = sorted(target.read_bytes().split(b"\n")[1:])
    ref = sorted(_reference_bytes(engine, frame, delimiter, tmp_path).split(b"\n")[1:])
    assert target.read_bytes().split(b"\n")[0] == delimiter.join(frame.columns).encode()
    assert got == ref


def test_empty_frame_writes_header_only(engine: DuckDBEngine, tmp_path: Path) -> None:
    frame = engine.create_dataframe([], "PATID string, TRIALID string")
    target = tmp_path / "e.csv"
    assert write_csv_file(engine, frame, str(target), delimiter="\t") == 0
    assert target.read_bytes() == b"PATID\tTRIALID\n"


def test_column_name_with_delimiter_still_nul_wrapped(engine: DuckDBEngine, tmp_path: Path) -> None:
    frame = engine.wrap(engine.connection().sql("SELECT 'v' AS \"a\tb\""))
    target = tmp_path / "w.csv"
    write_csv_file(engine, frame, str(target), delimiter="\t")
    assert target.read_bytes() == b"\x00a\tb\x00\nv\n"


def test_java_double_fast_path_matches_general_macro() -> None:
    """``__ovid2_java_double`` fast path == the general rewrite on fuzzed doubles."""
    import random
    import struct

    con = duckdb_mod().connect()
    _shortest_double_text(con)
    rng = random.Random(1)
    vals: list[float] = [
        1e-3,
        9.99999e-4,
        1e7,
        9999999.999,
        1e-4,
        -1e7,
        0.001,
        1234567.0,
        1e21,
        123e-10,
        5e-324,
        1.7976931348623157e308,
        float(2**53),
        0.1,
        0.30000000000000004,
        -0.0,
        0.0,
        math.nan,
        math.inf,
        -math.inf,
    ]
    for _ in range(50000):
        k = rng.random()
        if k < 0.4:
            vals.append(rng.uniform(-1e7, 1e7))
        elif k < 0.7:
            vals.append(round(rng.uniform(-1000, 1000), rng.randint(0, 6)))
        else:
            vals.append(struct.unpack("d", struct.pack("Q", rng.getrandbits(64)))[0])
    con.execute("CREATE TABLE t (x DOUBLE)")
    con.executemany("INSERT INTO t VALUES (?)", [(v,) for v in vals])
    bad = con.sql(
        "SELECT x FROM t WHERE __ovid2_java_double(x) IS DISTINCT FROM __ovid2_java_double_slow(x)"
    ).fetchall()
    assert bad == []


def duckdb_mod():  # type: ignore[no-untyped-def]
    import duckdb

    return duckdb
