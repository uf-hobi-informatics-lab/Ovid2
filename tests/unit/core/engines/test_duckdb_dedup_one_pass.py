"""DuckDB one-pass null-key-passthrough dedup equals the filter + union form."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

pytest.importorskip("duckdb")

from ovid2.core.engines import dsl
from ovid2.core.engines.duckdb import DuckDBEngine
from ovid2.transformations.deduplicators.base import Deduplicator

pytestmark = pytest.mark.unit

_ROWS = [
    ("1", "A", "x"),
    ("1", "A", "x"),  # exact duplicate
    ("1", "A", "y"),  # key duplicate, other payload
    ("1", "B", "x"),
    (None, "A", "n1"),  # null key part: always kept
    (None, "A", "n1"),
    ("", "A", "e1"),  # empty key part: always kept
    ("", "A", "e1"),
    ("2", None, "n2"),
    ("3", "C", "z"),
]


@pytest.fixture()
def engine() -> Iterator[DuckDBEngine]:
    eng = DuckDBEngine(args={"threads": 2, "memory_limit": "1GB"})
    dsl.set_active_engine(eng)
    yield eng
    eng.stop_session()
    dsl.set_active_engine(None)


def _df(engine: DuckDBEngine):  # type: ignore[no-untyped-def]
    return engine.create_dataframe(_ROWS, "ID string, K string, V string")


def _union_reference(engine: DuckDBEngine) -> list[tuple[object, ...]]:
    fns = engine.get_functions()
    df = _df(engine)
    pred = (fns.col("ID").isNotNull() & (fns.col("ID") != fns.lit(""))) & (
        fns.col("K").isNotNull() & (fns.col("K") != fns.lit(""))
    )
    deduped = engine.drop_duplicates(df.filter(pred), ["ID", "K"])
    out = engine.union_by_name(deduped, df.filter(~pred))
    return [tuple(r) for r in out.collect()]


def _key(row: tuple[object, ...]) -> tuple[str, str]:
    return (str(row[0]), str(row[1]))


def test_one_pass_matches_union_on_keys_and_counts(engine: DuckDBEngine) -> None:
    ref = _union_reference(engine)
    dedup = Deduplicator("t", key_fields=["ID", "K"], engine=engine)
    got = [tuple(r) for r in dedup.deduplicate(_df(engine)).collect()]
    assert len(got) == len(ref) == 8
    assert sorted(map(_key, got)) == sorted(map(_key, ref))
    # incomplete-key rows pass through untouched (all copies)
    incomplete = [r for r in got if r[0] in (None, "") or r[1] in (None, "")]
    assert len(incomplete) == 5


def test_one_pass_plan_has_no_union(engine: DuckDBEngine) -> None:
    out = Deduplicator("t", key_fields=["ID", "K"], engine=engine).deduplicate(_df(engine))
    assert "UNION" not in out.sql_query().upper()
