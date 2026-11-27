"""DuckDB SQL UDF registration survives an abandoned / interrupted stream.

Regression for the cancelled ACME-on-DuckDB job (2026-11-24): after a
cancel interrupted a ``to_arrow_batches`` stream, every later
``register_sql_functions`` failed with ``Catalog Error: Scalar Function
with name "legacy_format_date" already exists!`` — DuckDB 1.5 cannot
re-create a removed function while the connection holds a partially
consumed result.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

pytest.importorskip("duckdb")

from ovid2.core.engines import dsl
from ovid2.core.engines.duckdb import DuckDBEngine
from ovid2.legacy.udfs import SQL_FUNCTIONS, register_sql_functions

pytestmark = pytest.mark.unit


@pytest.fixture()
def engine() -> Iterator[DuckDBEngine]:
    eng = DuckDBEngine(args={"threads": 2, "memory_limit": "1GB"})
    dsl.set_active_engine(eng)
    yield eng
    eng.stop_session()
    dsl.set_active_engine(None)


def _partially_consume(engine: DuckDBEngine) -> None:
    session = engine.get_session()
    frame = session.sql(
        "SELECT legacy_format_date(CAST(i AS VARCHAR)) AS d FROM range(2000000) t(i)"
    )
    batches = frame.to_arrow_batches(1000)
    next(batches)
    engine.cancel_running_queries()
    batches.close()


def test_reregister_after_interrupted_stream(engine: DuckDBEngine) -> None:
    session = engine.get_session()
    register_sql_functions(session, "demo")
    _partially_consume(engine)
    # Each legacy table re-registers; this used to raise CatalogException.
    register_sql_functions(session, "demo")
    register_sql_functions(session, "demo")
    out = session.sql("SELECT legacy_format_date('2020-01-02') AS d").collect()
    assert len(out) == 1


def test_register_replaces_a_different_udf(engine: DuckDBEngine) -> None:
    session = engine.get_session()
    session.udf.register("ovid2_t_fn", lambda v: f"a{v}", "string")
    session.udf.register("ovid2_t_fn", lambda v: f"b{v}", "string")
    assert [tuple(r) for r in session.sql("SELECT ovid2_t_fn('x')").collect()] == [("bx",)]


def test_stop_session_forgets_registrations(engine: DuckDBEngine) -> None:
    register_sql_functions(engine.get_session(), "demo")
    engine.stop_session()
    # A fresh connection must get the functions again (no stale skip).
    session = engine.get_session()
    register_sql_functions(session, "demo")
    for name in SQL_FUNCTIONS:
        session.sql(f"SELECT {name}(NULL)").collect()
