"""Algorithm conformance: every algorithm gives the same rows on every engine.

Parametrised over **every algorithm in** ``AlgorithmRegistry`` × **every
engine whose capabilities cover its** ``requires``. Each algorithm ships a
data-only fixture (``tests/fixtures/algorithms/<kind>/<name>/case.yaml``:
input tables + expected rows, see ``tests/integration/_conformance.py``);
the test runs it on the engine and compares with the fixture after
normalising row order and float tolerance.

Engines: ``pyspark`` (shared ``spark`` fixture) and ``duckdb`` always run;
``snowpark`` runs when ``OVID2_TEST_SNOWPARK_ENGINE=<engine config>`` names a
live engine (``-m real_connection`` semantics, like the DSL parity suite);
``databricks_spark`` runs when ``OVID2_TEST_DATABRICKS_ENGINE`` is set.

Known traps covered explicitly by fixtures (search ``note:``): null vs empty
string (``standardize_null``, ``coalesce_columns``, the deduplicator's
null-key passthrough), date parsing / formatting and time zones
(``future_date_to_null``, ``illogical_*``, health ``future_date``), string
case (``to_title``, ``case_map``), decimal rounding (``clamp``, ``reduce``,
health ``null_rate``), and seeded randomness in dedup (the salted two-stage
dedup must keep exactly one row per key whatever the salt).

A semantic difference between engines is fixed in the engine's function
implementation, never in the algorithm or the fixture.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.integration import _conformance as conf

pytest.importorskip("duckdb")

pytestmark = [pytest.mark.integration, pytest.mark.conformance]

_ENGINE_TYPES: tuple[str, ...] = ("pyspark", "duckdb", "snowpark", "databricks_spark")


def _matrix() -> list[Any]:
    from ovid2.algorithms.registry import registry
    from ovid2.config.capabilities import ENGINE_CAPABILITIES

    params: list[Any] = []
    for algorithm in registry.list():
        kind, name = algorithm.kind.value, algorithm.name
        case = conf.load_case(kind, name)
        needed = set(algorithm.requires)
        if case is not None:
            requires_for = getattr(algorithm, "requires_for", None)
            if callable(requires_for):
                needed |= set(requires_for(case.params))
        for engine_type in _ENGINE_TYPES:
            have = {c.value for c in ENGINE_CAPABILITIES[engine_type]}
            marks: list[Any] = []
            if engine_type in ("snowpark", "databricks_spark"):
                marks.append(pytest.mark.real_connection)
            if not needed <= have:
                missing = sorted(needed - have)
                marks.append(
                    pytest.mark.skip(reason=f"{engine_type} lacks {missing} (refused at plan time)")
                )
            elif case is not None and case.engines and engine_type not in case.engines:
                marks.append(pytest.mark.skip(reason=f"fixture limited to {case.engines}"))
            params.append(
                pytest.param(
                    kind, name, engine_type, id=f"{kind}:{name}@{engine_type}", marks=marks
                )
            )
    return params


@pytest.fixture(scope="module")
def engines(spark: Any) -> Iterator[dict[str, Any]]:
    from ovid2.config.settings import SPARK_CONFIG
    from ovid2.core.engines.registry import load_engine_class

    previous = spark.conf.get("spark.sql.legacy.timeParserPolicy")
    spark.conf.set(
        "spark.sql.legacy.timeParserPolicy", SPARK_CONFIG["spark.sql.legacy.timeParserPolicy"]
    )
    py = load_engine_class("pyspark")()
    py._session = spark
    duck = load_engine_class("duckdb")(args={"threads": 2, "memory_limit": "2GB"})
    out: dict[str, Any] = {"pyspark": py, "duckdb": duck}
    remote: list[Any] = []
    for engine_type, env in (
        ("snowpark", "OVID2_TEST_SNOWPARK_ENGINE"),
        ("databricks_spark", "OVID2_TEST_DATABRICKS_ENGINE"),
    ):
        name = os.environ.get(env, "").strip()
        if not name:
            continue
        from ovid2.core.loaders import load_engine

        live = load_engine(name)
        if engine_type == "snowpark":
            warehouse = os.environ.get("OVID2_TEST_SNOWPARK_WAREHOUSE", "").strip()
            if warehouse:
                live.native().use_warehouse(warehouse)
            live.prepare_workspace(
                [
                    d
                    for d in os.environ.get("OVID2_TEST_SNOWPARK_DATABASE", "").split(",")
                    if d.strip()
                ]
            )
        out[engine_type] = live
        remote.append(live)
    yield out
    duck.stop_session()
    for live in remote:
        live.stop_session()
    spark.conf.set("spark.sql.legacy.timeParserPolicy", previous)


def test_every_algorithm_has_a_fixture() -> None:
    """Each algorithm in the registry ships a conformance fixture."""
    from ovid2.algorithms.registry import registry

    missing = [
        f"{a.kind.value}:{a.name}"
        for a in registry.list()
        if conf.load_case(a.kind.value, a.name) is None
    ]
    assert not missing, "add tests/fixtures/algorithms/<kind>/<name>/case.yaml for: " + ", ".join(
        missing
    )


def test_no_orphan_fixtures() -> None:
    """Every fixture names a registered algorithm."""
    from ovid2.algorithms.registry import registry

    orphans = [
        f"{p.parent.parent.name}:{p.parent.name}"
        for p in conf.FIXTURE_ROOT.glob("*/*/case.yaml")
        if not registry.has(p.parent.parent.name, p.parent.name)
    ]
    assert not orphans, orphans


@pytest.mark.parametrize(("kind", "name", "engine_type"), _matrix())
def test_algorithm_conformance(
    kind: str, name: str, engine_type: str, engines: dict[str, Any], tmp_path: Path
) -> None:
    engine = engines.get(engine_type)
    if engine is None:
        pytest.skip(f"no live {engine_type} engine configured")
    case = conf.load_case(kind, name)
    if case is None:
        pytest.skip("no fixture (test_every_algorithm_has_a_fixture fails)")
    columns, rows = conf.run_case(engine, case, tmp_path)
    expected_cols = [str(c) for c in case.expected.get("columns") or columns]
    if case.compare == "rows":
        assert [c.lower() for c in columns] == [c.lower() for c in expected_cols], (
            f"{kind}:{name}@{engine_type} columns {columns} != {expected_cols}"
        )
    got = conf.normalise(rows)
    want = conf.normalise([tuple(r) for r in case.expected.get("rows") or []])
    assert conf.rows_equal(got, want, case.float_tol), (
        f"{kind}:{name}@{engine_type} rows differ\n got: {got}\nwant: {want}\nfixture: {case.path}"
    )
