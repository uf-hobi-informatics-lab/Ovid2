"""``csv_text.write_cell_sql`` (DuckDB SQL) equals ``write_cell`` on every value.

The DuckDB engine renders legacy writer text natively with
:func:`ovid2.core.engines.csv_text.write_cell_sql`; it must produce the
exact text of the univocity writer model :func:`write_cell` (itself
pinned to Spark by ``test_csv_text.py``) for every value: NUL, tabs,
CR/LF, backslashes, the comment char ``#`` at a line start, whitespace
at either end, whitespace-only, empty and NULL — for tab and comma
delimiters.
"""

from __future__ import annotations

import random

import pytest

from ovid2.core.engines.csv_text import write_cell, write_cell_sql

duckdb = pytest.importorskip("duckdb")

pytestmark = pytest.mark.unit

_ALPHA = ["a", " ", "\t", "\x00", "\\", "#", "\n", "\r", '"', "\x1f", "é", "\x7f", "\x01", "x", ","]


def _values() -> list[str | None]:
    rnd = random.Random(3)
    vals: list[str | None] = [None, "", " ", "#", "#x", "a#", " #", "\t#", "x\t", "\\"]
    vals += ["".join(rnd.choice(_ALPHA) for _ in range(rnd.randint(0, 9))) for _ in range(60_000)]
    return vals


@pytest.mark.parametrize("delimiter", ["\t", ","])
@pytest.mark.parametrize("line_start", [True, False])
def test_write_cell_sql_matches_write_cell(delimiter: str, line_start: bool) -> None:
    vals = _values()
    conn = duckdb.connect()
    conn.execute("CREATE TABLE f (i INTEGER, v VARCHAR)")
    conn.executemany("INSERT INTO f VALUES (?, ?)", list(enumerate(vals)))
    expr = write_cell_sql("v", delimiter=delimiter, line_start=line_start)
    got = [r[0] for r in conn.execute(f"SELECT {expr} FROM f ORDER BY i").fetchall()]
    bad = [
        (v, g, e)
        for v, g in zip(vals, got, strict=True)
        if g != (e := write_cell(v, delimiter=delimiter, line_start=line_start))
    ]
    assert not bad, bad[:5]
