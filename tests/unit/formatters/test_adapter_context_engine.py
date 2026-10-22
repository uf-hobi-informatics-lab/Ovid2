"""Unit tests for the Phase-2 engine-portability threading on
:class:`AdapterContext`.

These assert the context carries an optional ``engine`` and exposes a
``functions`` convenience property, and that the pipeline / loader plumb
the engine through. They are pure-Python (no Spark session), so they
carry the ``unit`` marker and run on any host.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ovid2.formatters.adapters.base import AdapterContext


@pytest.mark.unit
def test_adapter_context_engine_defaults_to_none() -> None:
    """Back-compat: constructing a context without an engine is legal."""
    ctx = AdapterContext(
        spark=MagicMock(),
        input_address=MagicMock(),
        pipeline="pcornet_to_omop",
        batch_id="b1",
    )
    assert ctx.engine is None


@pytest.mark.unit
def test_functions_property_returns_engine_functions() -> None:
    """``ctx.functions`` delegates to ``engine.get_functions()``."""
    fake_functions = object()
    fake_engine = MagicMock()
    fake_engine.get_functions.return_value = fake_functions
    ctx = AdapterContext(
        spark=MagicMock(),
        input_address=MagicMock(),
        pipeline="p",
        batch_id="b",
        engine=fake_engine,
    )
    assert ctx.functions is fake_functions
    fake_engine.get_functions.assert_called_once_with()


@pytest.mark.unit
def test_functions_property_raises_without_engine() -> None:
    """Reaching for the DSL with no engine raises a clear error."""
    ctx = AdapterContext(
        spark=MagicMock(),
        input_address=MagicMock(),
        pipeline="p",
        batch_id="b",
    )
    with pytest.raises(RuntimeError, match="no engine attached"):
        _ = ctx.functions


@pytest.mark.unit
def test_build_formatter_threads_engine_onto_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_formatter`` places the engine on the AdapterContext."""
    from ovid2.formatters import engine as engine_mod

    captured: dict[str, object] = {}

    class _FakeSpec:
        pass

    def fake_find_and_load(pipeline: str, table: str) -> object:
        return _FakeSpec()

    class _FakeSpecFormatter:
        def __init__(self, spec: object, ctx: AdapterContext) -> None:
            captured["ctx"] = ctx

    monkeypatch.setattr(engine_mod, "find_and_load", fake_find_and_load)
    monkeypatch.setattr(engine_mod, "_SpecFormatter", _FakeSpecFormatter)

    fake_engine = MagicMock()
    engine_mod.build_formatter(
        pipeline="pcornet_to_omop",
        table="person",
        spark=MagicMock(),
        input_address=MagicMock(),
        batch_id="b1",
        engine=fake_engine,
    )
    ctx = captured["ctx"]
    assert isinstance(ctx, AdapterContext)
    assert ctx.engine is fake_engine


@pytest.mark.unit
def test_run_pipeline_puts_engine_in_transform_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_pipeline`` includes 'engine' in the transform-context dict.

    Exercises the plumbing without Spark: a Spark-family engine whose
    ``get_session`` returns a fake session, a formatter that returns a
    fake DataFrame, and a single recording transformer that captures the
    context it is handed.
    """
    from ovid2.core import pipeline as pipeline_mod

    # Avoid the reference-table warm-up branch and StorageLevel import
    # path by stubbing resolve_cdm_for_pipeline to a CDM with no
    # reference tables.
    fake_cdm = MagicMock()
    fake_cdm.reference_tables = set()
    monkeypatch.setattr(pipeline_mod, "resolve_cdm_for_pipeline", lambda pipeline: fake_cdm)
    monkeypatch.setattr(pipeline_mod, "_register_auxiliary_tables", lambda *a, **k: None)

    fake_df = MagicMock()
    monkeypatch.setattr(
        pipeline_mod,
        "_read_with_pipeline_hook",
        lambda **kwargs: fake_df,
    )

    fake_session = MagicMock()
    fake_engine = MagicMock()
    fake_engine.get_session.return_value = fake_session

    class _Formatter:
        def format(self, df: object) -> object:
            return fake_df

    captured: dict[str, object] = {}

    class _RecordingTransformer:
        def transform(self, df: object, context: dict) -> object:
            captured.update(context)
            return df

    output_address = MagicMock()

    pipeline_mod.run_pipeline(
        engine=fake_engine,
        input_address=MagicMock(),
        output_address=output_address,
        table_name="person",
        formatter=_Formatter(),
        transformations=[_RecordingTransformer()],
        pipeline="pcornet_to_omop",
        batch_id="b1",
        event_bus=MagicMock(),
        instance_id="i1",
    )

    assert "engine" in captured
    assert captured["engine"] is fake_engine


@pytest.mark.unit
def test_read_hook_optional_primary_missing_degrades_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing *optional* primary source yields an empty DF, not a raise.

    Regression: ACME ``pcornet_trial`` declares ``optional: true`` on its
    sole source (``*PREVENTABLE*``). Batches without a trial extract used
    to fail the whole table because the pipeline's eager placeholder read
    raised ``FileNotFoundError`` before the adapter's optional-aware read
    ran. The hook now consults ``formatter.primary_source_optional`` and
    returns an empty DataFrame so the table produces an empty output.
    """
    pytest.importorskip("pyspark")
    from ovid2.core import pipeline as pipeline_mod

    class _MissingAddress:
        def read(self, *a: object, **k: object) -> object:
            raise FileNotFoundError("No files found matching pattern '*PREVENTABLE*'")

    class _OptionalFormatter:
        input_file_name = "*PREVENTABLE*"
        primary_source_optional = True

    empty_marker = object()
    fake_session = MagicMock()
    fake_session.createDataFrame.return_value = empty_marker

    # No pipeline_settings module for a made-up pipeline → no hooks / datavant.
    result = pipeline_mod._read_with_pipeline_hook(
        spark=fake_session,
        input_address=_MissingAddress(),
        table_name="pcornet_trial",
        formatter=_OptionalFormatter(),
        pipeline="acme_from_disc",
        batch_id="20260610",
        bus=MagicMock(),
        instance_id="i1",
    )

    assert result is empty_marker
    assert fake_session.createDataFrame.called


@pytest.mark.unit
def test_read_hook_required_primary_missing_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing *required* primary source still raises ``FileNotFoundError``."""
    from ovid2.core import pipeline as pipeline_mod

    class _MissingAddress:
        def read(self, *a: object, **k: object) -> object:
            raise FileNotFoundError("No files found matching pattern '*demographic*'")

    class _RequiredFormatter:
        input_file_name = "*demographic*"
        primary_source_optional = False

    with pytest.raises(FileNotFoundError):
        pipeline_mod._read_with_pipeline_hook(
            spark=MagicMock(),
            input_address=_MissingAddress(),
            table_name="demographic",
            formatter=_RequiredFormatter(),
            pipeline="acme_from_disc",
            batch_id="20260610",
            bus=MagicMock(),
            instance_id="i1",
        )
