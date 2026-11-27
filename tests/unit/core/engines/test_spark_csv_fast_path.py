"""The native DuckDB CSV split equals the univocity port on every record.

``duckdb_stager.spark_csv_fields_sql`` splits most records in SQL and
sends only the rest to :func:`ovid2.core.engines.csv_text.parse_line`.
These tests run the real SQL in DuckDB over fuzzed records (quotes,
escapes, NUL, ``\\1``, delimiters inside quotes, whitespace, comment
chars) and over the Spark 4.1 reader oracle, with the Python fallback
registered, and require the result to equal ``parse_line`` on every
record — the fast path may never change a value.
"""

from __future__ import annotations

import gzip
import json
import random
from pathlib import Path

import pytest

from ovid2.core.engines.csv_text import is_blank_line, parse_line
from ovid2.core.engines.duckdb_stager import spark_csv_fields_sql

duckdb = pytest.importorskip("duckdb")

pytestmark = pytest.mark.unit

_ORACLE = Path(__file__).parent / "data" / "spark_csv_oracle.json.gz"
_DIALECTS = [(",", '"', "\\"), (",", '"', '"'), ("\t", '"', '"'), ("\t", '"', "\\")]
_WIDTH = 4


def _fuzz(delimiter: str, quote: str, n: int, seed: int) -> list[str]:
    rnd = random.Random(seed)
    alpha = ["a", "b", " ", ",", "\t", '"', "\\", "#", "\x00", "\x01", "é", "x", "\r"]
    lines: list[str] = []
    for _ in range(n):
        lines.append(
            "".join(
                rnd.choice([*alpha, delimiter, quote, quote]) for _ in range(rnd.randint(0, 12))
            )
        )
    for _ in range(n):
        fields = []
        for _ in range(rnd.randint(1, 7)):
            body = "".join(
                rnd.choice(["a", " ", "#", "é", "\\", delimiter, "x", "\x01"])
                for _ in range(rnd.randint(0, 5))
            )
            fields.append(
                quote + body + quote if rnd.random() < 0.6 else body.replace(delimiter, "")
            )
        lines.append(delimiter.join(fields))
    return [ln for ln in lines if not is_blank_line(ln)]


def _split_all(lines: list[str], delimiter: str, quote: str, escape: str) -> list[list[str | None]]:
    conn = duckdb.connect()

    def _parse(rec: str | None) -> list[str | None] | None:
        if rec is None:
            return None
        return parse_line(rec, width=_WIDTH, escape=escape, delimiter=delimiter, quote=quote)

    conn.create_function("__exact", _parse, ["VARCHAR"], "VARCHAR[]", null_handling="special")
    conn.execute("CREATE TABLE t (i INTEGER, l VARCHAR)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", list(enumerate(lines)))
    expr = spark_csv_fields_sql(
        "l", width=_WIDTH, delimiter=delimiter, quote=quote, escape=escape, parser_sql="__exact(l)"
    )
    rows = conn.execute(f"SELECT {expr} FROM t ORDER BY i").fetchall()
    return [[None if v in ("", None) else v for v in r[0]] for r in rows]


@pytest.mark.parametrize(("delimiter", "quote", "escape"), _DIALECTS)
def test_fast_path_matches_parser_on_fuzz(delimiter: str, quote: str, escape: str) -> None:
    lines = _fuzz(delimiter, quote, 20_000, seed=hash((delimiter, escape)) & 0xFFFF)
    got = _split_all(lines, delimiter, quote, escape)
    bad = [
        (ln, g, exp)
        for ln, g in zip(lines, got, strict=True)
        if g
        != (exp := parse_line(ln, width=_WIDTH, escape=escape, delimiter=delimiter, quote=quote))
    ]
    assert not bad, bad[:5]


@pytest.mark.parametrize("escape", ['"', "\\"])
def test_fast_path_matches_spark_reader_oracle(escape: str) -> None:
    with gzip.open(_ORACLE, "rt", encoding="utf-8") as fh:
        reader = json.load(fh)["reader"]
    slot = 2 if escape == '"' else 3
    rows = [row for row in reader if row[1] == _WIDTH and not is_blank_line(row[0])]
    assert rows, "oracle has no width-4 records"
    got = _split_all([row[0] for row in rows], "\t", '"', escape)
    bad = [(row[0], g, row[slot]) for row, g in zip(rows, got, strict=True) if g != row[slot]]
    assert not bad, bad[:5]


def test_simple_quoted_records_skip_the_python_parser() -> None:
    conn = duckdb.connect()
    calls: list[str] = []

    def _parse(rec: str | None) -> list[str | None]:
        calls.append(str(rec))
        return [None] * _WIDTH

    conn.create_function("__exact", _parse, ["VARCHAR"], "VARCHAR[]", null_handling="special")
    expr = spark_csv_fields_sql(
        "l", width=_WIDTH, delimiter=",", quote='"', escape="\\", parser_sql="__exact(l)"
    )
    sql = f"SELECT {expr} FROM (VALUES (?), (?)) v(l)"
    rows = conn.execute(sql, ['"148127","8507","","x,y"', '"a\\"b",c']).fetchall()
    assert rows[0][0] == ["148127", "8507", "", "x,y"]
    assert calls == ['"a\\"b",c']
