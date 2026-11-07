"""Java datetime -> DuckDB translation matches Spark LEGACY parsing.

The fixture ``data/spark_legacy_datetime.json`` was recorded from a real
Spark 4.1 session with ``spark.sql.legacy.timeParserPolicy=LEGACY`` (the
production setting) for ``to_date`` / ``to_timestamp`` (with and without
a pattern) and ``CAST(x AS DATE)`` over a corpus of clean and dirty
inputs. Every case is replayed through
:mod:`ovid2.core.engines._java_datetime` on an in-process DuckDB.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

from ovid2.core.engines import _java_datetime as jd  # noqa: E402

_CASES = json.loads(
    (Path(__file__).parent / "data" / "spark_legacy_datetime.json").read_text(encoding="utf-8")
)["cases"]


def _sql_for(kind: str, fmt: str | None) -> str:
    if kind == "cast_date":
        return f"CAST({jd.cast_sql('v', timestamp=False)} AS VARCHAR)"
    if fmt is None:
        expr = jd.cast_sql("v", timestamp=(kind == "t"))
    else:
        expr = jd.parse_sql("v", fmt)
        if kind == "d":
            expr = f"CAST({expr} AS DATE)"
    if kind == "t":
        return f"strftime({expr}, '%Y-%m-%d %H:%M:%S') || CASE WHEN microsecond({expr}) % 1000000 <> 0 THEN '.' || lpad(CAST(((microsecond({expr}) % 1000000) // 1000) AS VARCHAR), 3, '0') ELSE '' END"
    return f"CAST({expr} AS VARCHAR)"


def _split(key: str) -> tuple[str, str | None, str | None]:
    if key.startswith("cast_date|"):
        value = key.split("|", 1)[1]
        return "cast_date", None, None if value == "None" else value
    kind, fmt, value = key.split("|", 2)
    return kind, None if fmt == "None" else fmt, None if value == "None" else value


@pytest.fixture(scope="module")
def con() -> object:
    return duckdb.connect()


def _all_keys() -> list[str]:
    # Every value present in the corpus, including the ones Spark mapped
    # to NULL (absent from the JSON -> expected None).
    values = sorted({_split(k)[2] or "" for k in _CASES} | {"abc", "2023-13-01", "2023-02-30"})
    kinds = sorted({k.rsplit("|", 1)[0] for k in _CASES})
    keys = []
    for prefix in kinds:
        for v in values:
            keys.append(f"{prefix}|{v}")
    return keys


@pytest.mark.parametrize("key", _all_keys())
def test_matches_spark_legacy(con: object, key: str) -> None:
    kind, fmt, value = _split(key)
    if key == "t|None|12:30:45":
        pytest.skip("Spark fills today's date for a bare time; not reproducible")
    if fmt == "yyyy" and value == "20230415":
        pytest.skip("Spark LEGACY overflows (ArithmeticException) on this input")
    expected = _CASES.get(key)
    if isinstance(expected, str) and expected.startswith("+"):
        pytest.skip("Spark's +yyyyy rendering of 5-digit years")
    sql = f"SELECT {_sql_for(kind, fmt)} FROM (SELECT CAST(? AS VARCHAR) AS v)"
    got = con.execute(sql, [value]).fetchone()[0]  # type: ignore[attr-defined]
    assert got == expected, f"{key}: duckdb={got!r} spark={expected!r}"


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("yyyy-MM-dd", "%Y-%m-%d"),
        ("yyyy-MM-dd HH:mm:ss", "%Y-%m-%d %H:%M:%S"),
        ("M/d/yyyy", "%-m/%-d/%Y"),
        ("hh:mm a", "%I:%M %p"),
        ("dd-MMM-yyyy", "%d-%b-%Y"),
        ("yyyy-MM-dd'T'HH:mm:ss.SSS", "%Y-%m-%dT%H:%M:%S.%g"),
        ("yyyy-MM-dd 00:00:00", "%Y-%m-%d 00:00:00"),
        ("MMMM yy EEE", "%B %y %a"),
    ],
)
def test_strftime_format(pattern: str, expected: str) -> None:
    assert jd.strftime_format(pattern) == expected


def test_strftime_matches_spark_rendering(con: object) -> None:
    # Spark: date_format(to_timestamp('2023-04-05 03:04:05.123'), f)
    spark = {
        "yyyy-MM-dd": "2023-04-05",
        "yyyy-MM-dd HH:mm:ss": "2023-04-05 03:04:05",
        "M/d/yyyy": "4/5/2023",
        "HH:mm": "03:04",
        "hh:mm a": "03:04 AM",
        "dd-MMM-yyyy": "05-Apr-2023",
        "yyyyMMdd": "20230405",
        "yyyy-MM-dd'T'HH:mm:ss.SSS": "2023-04-05T03:04:05.123",
        "EEE": "Wed",
        "MMMM": "April",
        "yy": "23",
        "yyyy-MM-dd 00:00:00": "2023-04-05 00:00:00",
    }
    for pattern, want in spark.items():
        got = con.execute(  # type: ignore[attr-defined]
            "SELECT strftime(TIMESTAMP '2023-04-05 03:04:05.123', ?)",
            [jd.strftime_format(pattern)],
        ).fetchone()[0]
        assert got == want, pattern


def test_unsupported_letter_raises() -> None:
    with pytest.raises(jd.JavaPatternError):
        jd.parse_sql("v", "yyyy-MM-dd z")
