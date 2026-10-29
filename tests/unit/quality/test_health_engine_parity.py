"""Cross-engine parity for the data-health / EDC check operators.

Every operator in :mod:`ovid2.transformations.quality.ops` must produce the
*same* metric rows whatever compute engine runs it — storage, compute and
the check algorithms are orthogonal. This module builds one small fixture
CDM, runs every operator over it on each engine, and compares the results.

* ``spark`` — always runs (local Spark via the session fixture).
* ``snowpark`` — opt-in, ``real_connection``: needs a live Snowflake
  account. Enable with ``OVID2_TEST_SNOWPARK_ENGINE=<engine config name>``
  (e.g. ``snowpark``); the fixture tables are created as session-scoped
  temporary tables, so nothing persists in the account.

The Spark run is the oracle: Snowpark rows must match it exactly (metric,
source table, value, threshold, exception flag, details).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest

from ovid2.cdm.registry import get_cdm
from ovid2.transformations.quality.ops import OpContext, get_operator, operator_names
from ovid2.transformations.quality.registry import CheckSpec, get_spec

# --------------------------------------------------------------------------- #
# Fixture CDM — all string columns (the disc / native-read contract).          #
# --------------------------------------------------------------------------- #

_TABLES: dict[str, tuple[list[str], list[tuple[Any, ...]]]] = {
    "DEMOGRAPHIC": (
        ["PATID", "SEX", "BIRTH_DATE", "HISPANIC"],
        [
            ("P1", "M", "1980-01-01", "N"),
            ("P1", "F", "1980-01-01", "N"),
            ("P2", "ZZ", "1990-05-05", "Y"),
            ("P3", "F", None, "NI"),
            ("P4", "M", "2001-07-07", ""),
        ],
    ),
    "ENCOUNTER": (
        [
            "ENCOUNTERID",
            "PATID",
            "ADMIT_DATE",
            "DISCHARGE_DATE",
            "ENC_TYPE",
            "FACILITY_LOCATION",
        ],
        [
            ("E1", "P1", "2024-01-10", "2024-01-12", "IP", "32601"),
            ("E2", "P1", "2024-01-10", "2024-01-09", "IP", "3260A"),
            ("E3", "P2", "2024-02-01", None, "AV", "326"),
            ("E4", "P9", "2099-01-01", None, "ED", None),
            ("E5", "P3", "2024-03-03", "2024-03-04", "AV", "32607"),
            ("E6", "P3", "bogus", None, "OT", "32607"),
        ],
    ),
    "DIAGNOSIS": (
        ["DIAGNOSISID", "PATID", "ENCOUNTERID", "DX", "DX_TYPE", "ADMIT_DATE"],
        [
            ("D1", "P1", "E1", "I10", "10", "2024-01-10"),
            ("D2", "P1", "E2", "E11.9", "10", "2024-01-10"),
            ("D3", "P2", "E3", "250.00", "09", "2024-02-01"),
            ("D4", "P2", "E99", "X", "ZZ", "2024-02-01"),
        ],
    ),
    "PROCEDURES": (
        ["PROCEDURESID", "PATID", "ENCOUNTERID", "PX", "PX_TYPE", "PX_DATE"],
        [
            ("X1", "P1", "E1", "99213", "CH", "2024-01-10"),
            ("X2", "P2", "E3", "0016070", "10", "2024-02-01"),
        ],
    ),
    "VITAL": (
        ["VITALID", "PATID", "ENCOUNTERID", "HT", "WT", "MEASURE_DATE"],
        [
            ("V1", "P1", "E1", "70", "180", "2024-01-10"),
            ("V2", "P2", "E3", "abc", "9999", "2024-02-01"),
            ("V3", "P3", "E5", "12", "150", "2030-01-01"),
        ],
    ),
    "LAB_RESULT_CM": (
        ["LAB_RESULT_CM_ID", "PATID", "LAB_LOINC", "RESULT_DATE", "RESULT_UNIT", "RESULT_NUM"],
        [
            ("L1", "P1", "2345-7", "2024-01-10", "mg/dL", "95"),
            ("L2", "P1", "24323-8", "2024-01-11", None, "1"),
            ("L3", "P2", "", "2024-02-01", "NI", None),
        ],
    ),
    "PRESCRIBING": (
        ["PRESCRIBINGID", "PATID", "RXNORM_CUI", "RX_ORDER_DATE"],
        [
            ("R1", "P1", "38", "2024-01-10"),
            ("R2", "P2", "44", "2024-02-01"),
            ("R3", "P3", "nope", "2024-03-03"),
        ],
    ),
    "HARVEST": (
        ["NETWORKID", "REFRESH_DEMOGRAPHIC_DATE"],
        [("N1", "2024-04-01")],
    ),
}

#: Prior refresh for the freshness / refresh-trend operators.
_PRIOR: dict[str, tuple[list[str], list[tuple[Any, ...]]]] = {
    "DEMOGRAPHIC": (["PATID"], [("P1",), ("P2",), ("P3",), ("P4",), ("P5",), ("P6",)]),
    "ENCOUNTER": (["ENCOUNTERID"], [("E1",)]),
}

_RUN_DATE = date(2025, 1, 1)


def _spec_for(op_name: str) -> CheckSpec:
    """Return a registered spec exercising ``op_name`` (first match)."""
    from ovid2.transformations.quality.registry import load_all_specs

    for spec in load_all_specs():
        if spec.operator == op_name:
            return spec
    raise AssertionError(f"no spec uses operator {op_name!r}")


def _normalise(rows: list[Any]) -> list[tuple[Any, ...]]:
    """Comparable, order-independent view of a metric-row list."""
    out = []
    for r in rows:
        value = None if r.value_num is None else round(float(r.value_num), 4)
        thr = None if r.threshold_num is None else round(float(r.threshold_num), 4)
        out.append(
            (
                r.metric,
                r.source_table,
                value,
                r.value_str,
                thr,
                bool(r.exception_flag),
                repr(sorted((r.details or {}).items(), key=lambda kv: kv[0])),
            )
        )
    return sorted(out, key=repr)


# --------------------------------------------------------------------------- #
# Engines                                                                      #
# --------------------------------------------------------------------------- #


class _Harness:
    """One engine's view of the fixture CDM."""

    def __init__(self, name: str, ctx_factory: Any) -> None:
        self.name = name
        self.ctx = ctx_factory


def _spark_harness(spark: Any) -> _Harness:
    from ovid2.core.engines.pyspark import spark_functions

    frames = {t: spark.createDataFrame(rows, cols) for t, (cols, rows) in _TABLES.items()}
    prior = {t: spark.createDataFrame(rows, cols) for t, (cols, rows) in _PRIOR.items()}

    def _ctx() -> OpContext:
        return OpContext(
            session=spark,
            cdm=get_cdm("pcornet_7_0"),
            available_tables=frozenset(frames),
            count_fn=lambda df: int(df.count()),
            pipeline="test",
            run_date=_RUN_DATE,
            prior_tables=prior,
            functions=spark_functions(),
            frames=frames,
        )

    return _Harness("spark", _ctx)


@pytest.fixture(scope="module")
def snowpark_engine() -> Iterator[Any]:
    """Live Snowpark engine (opt-in via ``OVID2_TEST_SNOWPARK_ENGINE``)."""
    name = os.environ.get("OVID2_TEST_SNOWPARK_ENGINE", "").strip()
    if not name:
        pytest.skip("set OVID2_TEST_SNOWPARK_ENGINE=<engine config> for live Snowpark parity")
    from ovid2.core.loaders import load_engine

    engine = load_engine(name)
    try:
        engine.get_session()
        engine.prepare_workspace()
    except Exception as exc:
        pytest.skip(f"Snowpark engine '{name}' unavailable: {exc}")
    yield engine
    engine.stop_session()


def _snowpark_harness(engine: Any) -> _Harness:
    native = engine.native()

    def _table(cols: list[str], rows: list[tuple[Any, ...]]) -> Any:
        rows_s = [tuple(None if v is None else str(v) for v in r) for r in rows]
        df = engine.create_typed_dataframe(rows_s, [(c, "string") for c in cols])
        # Snapshot into a temp table so every operator scans a real table,
        # like a production native read.
        name = f"OVID2_PARITY_{abs(hash(tuple(cols))) % 10**8}"
        df.write.mode("overwrite").save_as_table(name, table_type="temporary")
        return native.table(name)

    frames = {t: _table(cols, rows) for t, (cols, rows) in _TABLES.items()}
    prior = {t: _table(cols, rows) for t, (cols, rows) in _PRIOR.items()}

    from ovid2.transformations.quality.runner import HealthCheckRunner

    runner = HealthCheckRunner(
        session=engine.get_session(),
        cdm=get_cdm("pcornet_7_0"),
        pipeline="test",
        batch_id="parity",
        engine=engine,
    )

    def _ctx() -> OpContext:
        return OpContext(
            session=engine.get_session(),
            cdm=get_cdm("pcornet_7_0"),
            available_tables=frozenset(frames),
            count_fn=lambda df: int(df.count()),  # noqa: OVID2001 test oracle
            pipeline="test",
            run_date=_RUN_DATE,
            prior_tables=prior,
            functions=engine.get_functions(),
            frames=frames,
            reference_reader=runner._read_reference,
        )

    return _Harness("snowpark", _ctx)


_OPERATORS = sorted(operator_names())


@pytest.mark.unit
def test_every_operator_is_covered() -> None:
    """New operators must be added to this parity matrix (via a registered spec)."""
    for op in _OPERATORS:
        assert _spec_for(op) is not None


@pytest.mark.parametrize("op_name", _OPERATORS)
def test_spark_operator_runs_on_fixture(spark: Any, op_name: str) -> None:
    """Every operator runs on the Spark engine via the portable context."""
    harness = _spark_harness(spark)
    fn = get_operator(op_name)
    assert fn is not None
    fn(harness.ctx(), _spec_for(op_name))


@pytest.mark.real_connection
@pytest.mark.parametrize("op_name", _OPERATORS)
def test_snowpark_matches_spark(spark: Any, snowpark_engine: Any, op_name: str) -> None:
    """Snowpark produces exactly the Spark metric rows for every operator."""
    spec = _spec_for(op_name)
    fn = get_operator(op_name)
    assert fn is not None
    expected = _normalise(fn(_spark_harness(spark).ctx(), spec))
    got = _normalise(fn(_snowpark_harness(snowpark_engine).ctx(), spec))
    assert got == expected


@pytest.mark.unit
def test_get_spec_resolves_fixture_ids() -> None:
    assert get_spec("1.01") is not None
