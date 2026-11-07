"""Unit tests for the non-Spark (Snowpark) leg of :func:`run_pipeline`.

Focus: the schema-context reset that keeps unqualified shared-view
references (e.g. ``session.table("concept")`` in the PCORnet→OMOP
formatters) resolvable across a multi-table run.

Regression guard for the ``Object 'CONCEPT' does not exist or not
authorized`` failure seen on a live Snowpark ``pcornet_to_omop`` run:
the previous table's ``stager.persist`` leaves the Snowpark session
pointed at the *output* schema (``USE SCHEMA <output>``), so the next
table's ``formatter.format`` would resolve the unqualified ``concept``
temp view against the wrong schema. ``_run_pipeline_staged`` must
re-bind the writable workspace namespace (``engine.prepare_workspace``)
*before* formatting, which restores the schema the shared view was
registered in.
"""

from __future__ import annotations

from typing import Any

from ovid2.core.pipeline import _run_pipeline_staged


class _RecordingEngine:
    """Minimal non-Spark engine double that records the call order."""

    session_is_spark = False
    type_name = "snowpark"

    def __init__(self, calls: list[str], session: Any, stager: Any) -> None:
        self._calls = calls
        self._session = session
        self._stager = stager

    def get_session(self) -> Any:
        return self._session

    def get_stager(self) -> Any:
        return self._stager

    def prepare_workspace(self) -> None:
        self._calls.append("prepare_workspace")


class _RecordingStager:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def materialise(self, *args: Any, **kwargs: Any) -> Any:
        self._calls.append("materialise")
        return object()

    def persist(self, *args: Any, **kwargs: Any) -> None:
        self._calls.append("persist")


class _RecordingFormatter:
    """Formatter double that records when ``format`` runs."""

    input_file_name = "*condition*"
    auxiliary_tables: tuple[str, ...] = ()

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def format(self, df: Any) -> Any:
        self._calls.append("format")
        return _ViewDataFrame()


class _ViewDataFrame:
    """DataFrame double exposing the Snowpark temp-view API."""

    def create_or_replace_temp_view(self, name: str) -> None:
        pass


class _NullBus:
    def emit(self, *args: Any, **kwargs: Any) -> None:
        pass


def test_prepare_workspace_runs_before_formatting(monkeypatch: Any) -> None:
    """``prepare_workspace`` must precede ``formatter.format`` on the staged leg.

    Otherwise unqualified shared-view references (``concept``) resolve
    against whatever schema the previous table's ``persist`` left the
    session bound to — the exact ``Object 'CONCEPT' does not exist``
    failure this test guards against.
    """
    calls: list[str] = []

    # ``driver_from_address`` is imported lazily inside the function;
    # patch it on the registry module so no real driver is resolved.
    import ovid2.drivers.registry as registry_mod

    monkeypatch.setattr(
        registry_mod,
        "driver_from_address",
        lambda *a, **k: object(),
    )

    session = object()
    stager = _RecordingStager(calls)
    engine = _RecordingEngine(calls, session, stager)
    formatter = _RecordingFormatter(calls)

    _run_pipeline_staged(
        engine=engine,  # type: ignore[arg-type]
        input_address=object(),  # type: ignore[arg-type]
        output_address=object(),  # type: ignore[arg-type]
        table_name="condition_occurrence",
        formatter=formatter,  # type: ignore[arg-type]
        transformations=[],
        pipeline="pcornet_to_omop",
        batch_id="PUBLIC",
        bus=_NullBus(),  # type: ignore[arg-type]
        instance_id="test-instance",
        cdm=object(),  # type: ignore[arg-type]
        input_address_name="19r2",
        output_address_name="19r2_omop",
        sf_overrides=None,
        cdm_name="omop_5_3",
    )

    assert "prepare_workspace" in calls, calls
    assert "format" in calls, calls
    # The workspace must be (re)bound after the source is materialised but
    # before formatting, so the shared-view schema is current when the
    # formatter resolves ``session.table("concept")``.
    assert calls.index("prepare_workspace") < calls.index("format"), calls
    # And formatting must complete before the result is persisted.
    assert calls.index("format") < calls.index("persist"), calls


def test_register_engine_view_uses_snowpark_view(monkeypatch: Any) -> None:
    """The finished table is registered via the engine-agnostic view helper.

    Sanity check that ``_run_pipeline_staged`` still registers the output
    table as a temp view (so cross-table fixers can reference it) after
    the workspace re-bind change.
    """
    calls: list[str] = []
    registered: list[str] = []

    import ovid2.drivers.registry as registry_mod

    monkeypatch.setattr(
        registry_mod,
        "driver_from_address",
        lambda *a, **k: object(),
    )

    class _ViewSession:
        def create_or_replace_temp_view(self, name: str) -> None:
            registered.append(name)

    session = _ViewSession()
    stager = _RecordingStager(calls)
    engine = _RecordingEngine(calls, session, stager)
    formatter = _RecordingFormatter(calls)

    # The result DataFrame is what ``format`` returns; give it the
    # Snowpark view API so ``_register_engine_view`` uses it.
    def _fmt(df: Any) -> Any:
        calls.append("format")
        return session

    formatter.format = _fmt  # type: ignore[assignment]

    _run_pipeline_staged(
        engine=engine,  # type: ignore[arg-type]
        input_address=object(),  # type: ignore[arg-type]
        output_address=object(),  # type: ignore[arg-type]
        table_name="condition_occurrence",
        formatter=formatter,  # type: ignore[arg-type]
        transformations=[],
        pipeline="pcornet_to_omop",
        batch_id="PUBLIC",
        bus=_NullBus(),  # type: ignore[arg-type]
        instance_id="test-instance",
        cdm=object(),  # type: ignore[arg-type]
        input_address_name="19r2",
        output_address_name="19r2_omop",
        sf_overrides=None,
        cdm_name="omop_5_3",
    )

    assert registered == ["condition_occurrence"], registered


def _run_staged(engine: Any, formatter: Any, table: str, cdm: Any) -> None:
    _run_pipeline_staged(
        engine=engine,
        input_address=object(),  # type: ignore[arg-type]
        output_address=object(),  # type: ignore[arg-type]
        table_name=table,
        formatter=formatter,
        transformations=[],
        pipeline="acme_from_disc",
        batch_id="b",
        bus=_NullBus(),  # type: ignore[arg-type]
        instance_id="test-instance",
        cdm=cdm,
        input_address_name="in",
        output_address_name="out",
        sf_overrides=None,
        cdm_name="pcornet_7_0",
    )


class _MaterialisingEngine(_RecordingEngine):
    """DuckDB-shaped double: exposes ``materialise`` like ``DuckDBEngine``."""

    type_name = "duckdb"

    def __init__(self, calls: list[str], session: Any, stager: Any) -> None:
        super().__init__(calls, session, stager)
        self.materialised: list[tuple[Any, str]] = []
        self.result = _ViewDataFrame()

    def materialise(self, df: Any, name: str) -> Any:
        self._calls.append("engine.materialise")
        self.materialised.append((df, name))
        return self.result


def test_reference_table_materialised_once_before_view_and_persist(monkeypatch: Any) -> None:
    """Regression (ACME on DuckDB, 2026-11-04): reference tables were lazy views.

    Every clinical table's cross-table fixers (``patid_orphans`` ->
    ``demographic``, ``encounterid_orphans`` -> ``encounter``) then
    re-executed the reference table's whole plan. The staged leg must
    materialise it once — and register *and* persist that same frame.
    """
    import ovid2.drivers.registry as registry_mod

    monkeypatch.setattr(registry_mod, "driver_from_address", lambda *a, **k: object())
    calls: list[str] = []
    persisted: list[Any] = []

    class _Stager(_RecordingStager):
        def persist(self, df: Any, *args: Any, **kwargs: Any) -> None:
            persisted.append(df)

    engine = _MaterialisingEngine(calls, object(), _Stager(calls))
    cdm = type("Cdm", (), {"reference_tables": ("demographic", "encounter")})()
    _run_staged(engine, _RecordingFormatter(calls), "encounter", cdm)
    assert [name for _df, name in engine.materialised] == ["ref_encounter"]
    assert persisted == [engine.result]

    engine.materialised.clear()
    _run_staged(engine, _RecordingFormatter(calls), "diagnosis", cdm)
    assert engine.materialised == []


def test_optional_primary_source_missing_yields_empty_table(monkeypatch: Any) -> None:
    """Regression (ACME ``pcornet_trial`` on DuckDB, 2026-11-07).

    An absent ``optional: true`` primary source (no ``*PREVENTABLE*``
    file in the batch) must produce an empty table with a warning, as
    the Spark placeholder read does — not fail the table.
    """
    import ovid2.drivers.registry as registry_mod

    monkeypatch.setattr(registry_mod, "driver_from_address", lambda *a, **k: object())
    calls: list[str] = []
    events: list[Any] = []

    class _MissingStager(_RecordingStager):
        def materialise(self, *args: Any, **kwargs: Any) -> Any:
            raise FileNotFoundError("No files found matching pattern '*PREVENTABLE*'")

    class _SentinelFrame:
        """DuckDB-like: cannot drop every column."""

        columns = ["__ovid2_empty__"]

        def drop(self, *cols: Any) -> Any:
            raise ValueError("drop: cannot drop every column of a DuckDB frame")

    class _Session:
        def createDataFrame(self, data: Any, schema: Any = None) -> Any:
            calls.append(f"createDataFrame:{schema}")
            return _SentinelFrame()

    class _Bus:
        def emit(self, event: Any) -> None:
            events.append(event)

    formatter = _RecordingFormatter(calls)
    formatter.primary_source_optional = True  # type: ignore[attr-defined]
    engine = _RecordingEngine(calls, _Session(), _MissingStager(calls))
    _run_pipeline_staged(
        engine=engine,  # type: ignore[arg-type]
        input_address=object(),  # type: ignore[arg-type]
        output_address=object(),  # type: ignore[arg-type]
        table_name="pcornet_trial",
        formatter=formatter,  # type: ignore[arg-type]
        transformations=[],
        pipeline="acme_from_disc",
        batch_id="b",
        bus=_Bus(),  # type: ignore[arg-type]
        instance_id="test-instance",
        cdm=object(),  # type: ignore[arg-type]
        input_address_name="in",
        output_address_name="out",
        sf_overrides=None,
        cdm_name="pcornet_7_0",
    )
    assert "format" in calls and "persist" in calls, calls
    assert any("producing an empty table" in str(e) for e in events), events


def test_required_primary_source_missing_still_fails(monkeypatch: Any) -> None:
    import pytest

    import ovid2.drivers.registry as registry_mod

    monkeypatch.setattr(registry_mod, "driver_from_address", lambda *a, **k: object())
    calls: list[str] = []

    class _MissingStager(_RecordingStager):
        def materialise(self, *args: Any, **kwargs: Any) -> Any:
            raise FileNotFoundError("missing")

    engine = _RecordingEngine(calls, object(), _MissingStager(calls))
    with pytest.raises(FileNotFoundError):
        _run_staged(
            engine,
            _RecordingFormatter(calls),
            "diagnosis",
            type("C", (), {"reference_tables": ()})(),
        )
