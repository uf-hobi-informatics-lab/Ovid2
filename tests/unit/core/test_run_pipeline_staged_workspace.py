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

    def create_or_replace_temp_view(self, name: str) -> None:  # noqa: D401
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
