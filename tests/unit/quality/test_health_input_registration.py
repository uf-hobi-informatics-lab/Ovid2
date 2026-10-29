"""Spark-free unit tests for ``HealthCheckRunner.register_input_tables``.

These cover the address-level failure surface (e.g. a Snowflake address with
no database): the real exception must reach the event log, and a wholesale
"no table could be read" must fail loudly instead of producing a deceptively
green health summary.
"""

from __future__ import annotations

from typing import Any

import pytest

from ovid2.core.events import Event
from ovid2.transformations.quality.runner import HealthCheckRunner


class _CapturingBus:
    """Minimal EventBus that records every emitted event."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)


class _RaisingAddress:
    """Input address whose every read raises — simulates empty-DB Snowflake."""

    def __init__(self, message: str) -> None:
        self._message = message

    def read(self, *args: Any, **kwargs: Any) -> Any:
        raise ValueError(self._message)


class _FakeDataFrame:
    def __init__(self, name: str) -> None:
        self.columns = ["PATID"]
        self._name = name
        self.registered_as: str | None = None

    def createOrReplaceTempView(self, name: str) -> None:
        self.registered_as = name


class _WorkingAddress:
    """Input address that returns a fake DataFrame for every table."""

    def read(self, session: Any, pipeline: str, batch_id: str, file_name: str, **kw: Any) -> Any:
        return _FakeDataFrame(file_name)


def _runner(bus: _CapturingBus) -> HealthCheckRunner:
    return HealthCheckRunner(
        session=object(),
        cdm=object(),  # type: ignore[arg-type]
        pipeline="DC_CHECKS",
        batch_id="20260915-233005",
        instance_id="test-instance",
        event_bus=bus,  # type: ignore[arg-type]
    )


@pytest.mark.unit
def test_register_surfaces_real_exception_in_warning() -> None:
    bus = _CapturingBus()
    runner = _runner(bus)
    address = _RaisingAddress("No Snowflake database specified")
    with pytest.raises(RuntimeError):
        runner.register_input_tables(address, ["DEMOGRAPHIC", "ENCOUNTER"])
    warnings = [e for e in bus.events if e.kind == "warning"]
    assert warnings, "expected per-table warning events"
    # The real reason must be carried through, not swallowed into a generic
    # "not available" message.
    msgs = " ".join(str(e.payload.get("message", "")) for e in warnings)
    assert "No Snowflake database specified" in msgs
    reasons = [e.payload.get("extra", {}).get("reason", "") for e in warnings]
    assert any("ValueError" in r for r in reasons)


@pytest.mark.unit
def test_register_raises_when_no_table_readable() -> None:
    bus = _CapturingBus()
    runner = _runner(bus)
    address = _RaisingAddress("boom")
    with pytest.raises(RuntimeError, match="0 of 3 source tables"):
        runner.register_input_tables(address, ["DEMOGRAPHIC", "ENCOUNTER", "DEATH"])


@pytest.mark.unit
def test_register_empty_candidates_returns_empty_without_raising() -> None:
    bus = _CapturingBus()
    runner = _runner(bus)
    available = runner.register_input_tables(_RaisingAddress("boom"), [])
    assert available == frozenset()


@pytest.mark.unit
def test_register_returns_available_when_reads_succeed() -> None:
    bus = _CapturingBus()
    runner = _runner(bus)
    available = runner.register_input_tables(_WorkingAddress(), ["DEMOGRAPHIC", "ENCOUNTER"])
    assert available == frozenset({"DEMOGRAPHIC", "ENCOUNTER"})
    assert not [e for e in bus.events if e.kind == "warning"]


# --------------------------------------------------------------------------- #
# not_applicable downgrade (SAS empty-table rule)                             #
# --------------------------------------------------------------------------- #


class _CountingSession:
    """Fake engine session mapping table name -> a fake DF with a row count."""

    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts

    def table(self, name: str) -> Any:
        return _CountingDataFrame(self._counts.get(name.upper(), 0))


class _CountingDataFrame:
    def __init__(self, n: int) -> None:
        self._n = n

    def count(self) -> int:
        return self._n


def _na_runner(session: Any) -> HealthCheckRunner:
    return HealthCheckRunner(
        session=session,
        cdm=object(),  # type: ignore[arg-type]
        pipeline="DC_CHECKS",
        batch_id="20260916-000000",
        instance_id="na-test",
        event_bus=_CapturingBus(),  # type: ignore[arg-type]
    )


def _register_stub_operator(name: str, returns_exception: bool) -> None:
    """Register a throwaway operator emitting one (optionally flagged) row."""
    from ovid2.transformations.quality.ops import MetricRow, operator

    @operator(name)
    def _stub(ctx: Any, spec: Any) -> list[MetricRow]:
        return [
            MetricRow(
                check_id=spec.id,
                check_name=spec.name,
                part=spec.part,
                edc_table=spec.edc_table,
                source_table=spec.source_tables[0] if spec.source_tables else None,
                metric="STUB",
                value_num=1.0,
                exception_flag=returns_exception,
            )
        ]


def _spec(operator_name: str, na_when_empty: tuple[str, ...]) -> Any:
    from ovid2.transformations.quality.registry import CheckSpec

    return CheckSpec(
        id="9.99",
        name="DC 9.99",
        part="part3",
        description="stub",
        operator=operator_name,
        source_tables=("PRESCRIBING",),
        na_when_empty=na_when_empty,
    )


@pytest.mark.unit
def test_na_downgrade_when_all_listed_tables_empty() -> None:
    """A flagged check downgrades to n/a when every na_when_empty table is empty."""
    _register_stub_operator("_stub_na_empty", returns_exception=True)
    runner = _na_runner(_CountingSession({"PRESCRIBING": 0}))
    spec = _spec("_stub_na_empty", na_when_empty=("PRESCRIBING",))
    outcomes = runner.run([spec], available_tables=frozenset({"PRESCRIBING"}))
    assert outcomes[0].status == "not_applicable"


@pytest.mark.unit
def test_na_downgrade_when_table_absent() -> None:
    """An absent (never-read) table counts as empty for the n/a gate."""
    _register_stub_operator("_stub_na_absent", returns_exception=False)
    runner = _na_runner(_CountingSession({}))
    spec = _spec("_stub_na_absent", na_when_empty=("PRESCRIBING",))
    outcomes = runner.run([spec], available_tables=frozenset())
    assert outcomes[0].status == "not_applicable"


@pytest.mark.unit
def test_no_na_downgrade_when_a_listed_table_populated() -> None:
    """If any na_when_empty table has rows, the computed status is kept."""
    _register_stub_operator("_stub_na_populated", returns_exception=True)
    runner = _na_runner(_CountingSession({"LAB_RESULT_CM": 5, "PRESCRIBING": 0, "VITAL": 0}))
    spec = _spec("_stub_na_populated", na_when_empty=("LAB_RESULT_CM", "PRESCRIBING", "VITAL"))
    outcomes = runner.run([spec], available_tables=frozenset({"LAB_RESULT_CM"}))
    assert outcomes[0].status == "exception"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))


# --------------------------------------------------------------------------- #
# Engine-portable registration (non-Spark engines read through the stager)    #
# --------------------------------------------------------------------------- #


class _RecordingStager:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, str]] = []

    def materialise(
        self, driver: Any, pipeline: str, batch_id: str, file_name: str, *a: Any, **kw: Any
    ) -> Any:
        self.calls.append((driver, file_name))
        return _FakeDataFrame(file_name)


class _FakeSnowparkEngine:
    session_is_spark = False

    def __init__(self) -> None:
        self.stager = _RecordingStager()
        self.typed: list[tuple[list[tuple[Any, ...]], list[tuple[str, str]]]] = []

    def get_stager(self) -> _RecordingStager:
        return self.stager

    def get_functions(self) -> Any:
        return object()

    def create_typed_dataframe(
        self, rows: list[tuple[Any, ...]], schema: list[tuple[str, str]]
    ) -> Any:
        self.typed.append((rows, schema))
        return "typed-frame"


@pytest.mark.unit
def test_non_spark_engine_reads_through_stager_and_skips_temp_views() -> None:
    bus = _CapturingBus()
    engine = _FakeSnowparkEngine()
    runner = HealthCheckRunner(
        session=object(),
        cdm=object(),  # type: ignore[arg-type]
        pipeline="p",
        batch_id="b",
        event_bus=bus,  # type: ignore[arg-type]
        engine=engine,  # type: ignore[arg-type]
    )
    driver = object()
    available = runner.register_input_tables(driver, ["DEMOGRAPHIC"])  # type: ignore[arg-type]
    assert available == frozenset({"DEMOGRAPHIC"})
    assert engine.stager.calls == [(driver, "demographic")]
    frame = runner.frames["DEMOGRAPHIC"]
    assert frame.registered_as is None  # no session temp view on Snowpark


@pytest.mark.unit
def test_results_frame_built_by_engine_with_portable_schema() -> None:
    from ovid2.transformations.quality.results import METRIC_ROW_COLUMNS
    from ovid2.transformations.quality.runner import RESULTS_SCHEMA

    engine = _FakeSnowparkEngine()
    runner = HealthCheckRunner(
        session=object(),
        cdm=object(),  # type: ignore[arg-type]
        pipeline="p",
        batch_id="b",
        instance_id="run-1",
        event_bus=_CapturingBus(),  # type: ignore[arg-type]
        engine=engine,  # type: ignore[arg-type]
    )
    assert runner.build_results_dataframe([]) == "typed-frame"
    _rows, schema = engine.typed[0]
    assert schema == RESULTS_SCHEMA
    assert tuple(name for name, _t in RESULTS_SCHEMA) == METRIC_ROW_COLUMNS


@pytest.mark.unit
def test_base_frame_counts_are_memoised_derived_are_not() -> None:
    runner = _runner(_CapturingBus())

    class _Counted:
        def __init__(self) -> None:
            self.calls = 0

        def count(self) -> int:
            self.calls += 1
            return 7

    base, derived = _Counted(), _Counted()
    runner.frames["DEMOGRAPHIC"] = base
    assert runner._count(base) == runner._count(base) == 7
    assert base.calls == 1
    runner._count(derived)
    runner._count(derived)
    assert derived.calls == 2
