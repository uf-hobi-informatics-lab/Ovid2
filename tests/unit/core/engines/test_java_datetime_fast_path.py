"""The native ``try_strptime`` fast path equals the general LEGACY parser.

The general (regexp + range-checked ``make_timestamp``) path is pinned
to Spark 4.1 LEGACY output by ``test_java_datetime.py``; this test
fuzzes the fast path against it on adversarial inputs.
"""

from __future__ import annotations

import random
from collections.abc import Iterator

import pytest

from ovid2.core.engines import _java_datetime as jd

duckdb = pytest.importorskip("duckdb")

pytestmark = pytest.mark.unit

_PATTERNS = ["yyyy-MM-dd", "yyyy-MM-dd HH:mm:ss", "yyyy/MM/dd", "yyyy-MM-dd HH:mm", "yyyy-M-d"]


def _general(value: str, pattern: str, monkeypatch: pytest.MonkeyPatch) -> str:
    with monkeypatch.context() as m:
        m.setattr(jd, "_fast_path", lambda *a: None)
        return jd.parse_sql(value, pattern)


def _values() -> Iterator[str]:
    rng = random.Random(7)
    fixed = [
        "2020-02-30 00:00:00",
        "2020-02-29 23:59:59",
        "2021-02-29",
        "2020-01-01 24:00:00",
        "2020-01-01 23:60:00",
        "2020-01-01 23:59:60",
        "0000-01-01",
        "9999-12-31 23:59:59",
        "2020-13-01",
        "2020-00-10",
        "2020-01-00",
        "2020-01-01 00:00:00.123",
        "2020-01-01x",
        "2020-01-001",
        "2020-01-01 00:00:001",
        "2020-01-01T00:00:00",
        "2020-1-1",
        " 2020-01-01",
        "",
        "2020/01/02",
        "20200102",
        "2020-01-02 3:04:05",
        "12020-01-01",
        "2020-01-01 00:00",
        "-2020-01-01",
    ]
    yield from fixed
    alphabet = "0123456789-/: Tx."
    for _ in range(3000):
        base = f"{rng.randint(0, 9999):04d}-{rng.randint(0, 13):02d}-{rng.randint(0, 32):02d}"
        if rng.random() < 0.6:
            base += f" {rng.randint(0, 25):02d}:{rng.randint(0, 61):02d}:{rng.randint(0, 61):02d}"
        if rng.random() < 0.3:
            pos = rng.randrange(len(base) + 1)
            base = base[:pos] + rng.choice(alphabet) + base[pos:]
        yield base


@pytest.mark.parametrize("pattern", _PATTERNS)
def test_fast_path_matches_general(pattern: str, monkeypatch: pytest.MonkeyPatch) -> None:
    con = duckdb.connect()
    con.execute("CREATE TABLE t (v VARCHAR)")
    con.executemany("INSERT INTO t VALUES (?)", [(v,) for v in _values()])
    fast = jd.parse_sql("v", pattern)
    general = _general("v", pattern, monkeypatch)
    bad = con.sql(
        f"SELECT v, {general} AS g, {fast} AS f FROM t WHERE ({general}) IS DISTINCT FROM ({fast})"
    ).fetchall()
    assert bad == []


def test_fast_path_only_for_full_canonical_dates() -> None:
    assert jd._fast_path("v", "yyyy-MM-dd") is not None
    assert jd._fast_path("v", "HH:mm:ss") is None
    assert jd._fast_path("v", "MM/dd/yyyy") is None
    assert jd._fast_path("v", "yyyyMMdd") is None
    assert jd._fast_path("v", "yy-MM-dd") is None
