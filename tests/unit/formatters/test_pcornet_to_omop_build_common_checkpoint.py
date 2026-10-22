"""Unit tests for the ``build_common`` checkpoint boundary (Change B).

These tests do not require Spark. They drive
:func:`ovid2_pipelines.pcornet_to_omop.formatters_custom._common.build_common`
with fake ``functions`` / ``engine`` / ``ctx`` objects and assert only the
one property that matters for the measurement-slowness fix: the fan-in is
handed to ``engine.checkpoint`` exactly once, with a name scoped to the
OMOP table being built.

The per-source column projections are exercised end-to-end by the real
Spark tests in ``test_pcornet_to_omop_m3_formatters.py``; here we only care
about the materialisation-boundary wiring.
"""

from __future__ import annotations

from typing import Any

import pytest

from ovid2_pipelines.pcornet_to_omop.formatters_custom import _common


class _FakeColumn:
    """A no-op column stand-in that supports the chained builder calls."""

    def alias(self, _name: str) -> "_FakeColumn":
        return self

    def otherwise(self, _o: Any) -> "_FakeColumn":
        return self

    def isNotNull(self) -> "_FakeColumn":  # noqa: N802 (Spark camelCase)
        return self

    def cast(self, _t: str) -> "_FakeColumn":
        return self

    def isin(self, *_a: Any) -> "_FakeColumn":
        return self

    def __eq__(self, _o: Any) -> "_FakeColumn":  # type: ignore[override]
        return self

    def __ne__(self, _o: Any) -> "_FakeColumn":  # type: ignore[override]
        return self

    def __and__(self, _o: Any) -> "_FakeColumn":
        return self

    def __invert__(self) -> "_FakeColumn":
        return self


class _FakeDF:
    """A DataFrame stand-in: every transform returns a fresh ``_FakeDF``."""

    def select(self, *_a: Any, **_k: Any) -> "_FakeDF":
        return _FakeDF()

    def filter(self, *_a: Any, **_k: Any) -> "_FakeDF":
        return _FakeDF()


class _FakeFunctions:
    """Just enough of the EngineFunctions DSL for the ``_common`` legs."""

    def col(self, _n: str) -> _FakeColumn:
        return _FakeColumn()

    def lit(self, _v: Any) -> _FakeColumn:
        return _FakeColumn()

    def trim(self, _c: Any) -> _FakeColumn:
        return _FakeColumn()

    def upper(self, _c: Any) -> _FakeColumn:
        return _FakeColumn()

    def concat(self, *_c: Any) -> _FakeColumn:
        return _FakeColumn()

    def concat_ws(self, *_c: Any) -> _FakeColumn:
        return _FakeColumn()

    def when(self, *_a: Any) -> _FakeColumn:
        return _FakeColumn()

    def coalesce(self, *_a: Any) -> _FakeColumn:
        return _FakeColumn()

    def current_timestamp(self) -> _FakeColumn:
        return _FakeColumn()

    def date_format(self, *_a: Any) -> _FakeColumn:
        return _FakeColumn()

    def to_date(self, *_a: Any) -> _FakeColumn:
        return _FakeColumn()

    def map_from_dict(self, *_a: Any) -> _FakeColumn:
        return _FakeColumn()


class _FakeEngine:
    """Records checkpoint calls; unions are pass-through."""

    def __init__(self) -> None:
        self.checkpoint_calls: list[tuple[Any, str]] = []

    def union_by_name(
        self, left: Any, _right: Any, *, allow_missing_columns: bool = False
    ) -> Any:
        return left

    def checkpoint(self, df: Any, name: str) -> Any:
        self.checkpoint_calls.append((df, name))
        return df


class _FakeCtx:
    """Adapter-context stand-in. Only LAB_RESULT_CM (primary) is present."""

    def __init__(self, functions: _FakeFunctions, engine: _FakeEngine) -> None:
        self._functions = functions
        self.engine = engine

    @property
    def functions(self) -> _FakeFunctions:
        return self._functions


@pytest.fixture()
def wiring() -> tuple[_FakeCtx, _FakeEngine]:
    fns = _FakeFunctions()
    engine = _FakeEngine()
    return _FakeCtx(fns, engine), engine


@pytest.mark.unit
def test_build_common_checkpoints_once(
    monkeypatch: pytest.MonkeyPatch, wiring: tuple[_FakeCtx, _FakeEngine]
) -> None:
    """The fan-in is handed to ``engine.checkpoint`` exactly once."""
    ctx, engine = wiring
    # No sibling sources present — every _read raises FileNotFoundError so
    # only the LAB primary leg contributes. This isolates the checkpoint.
    monkeypatch.setattr(
        _common, "_read", lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError())
    )
    result = _common.build_common(_FakeDF(), ctx, table_name="measurement")

    assert len(engine.checkpoint_calls) == 1
    assert result is engine.checkpoint_calls[0][0]


@pytest.mark.unit
def test_build_common_checkpoint_name_is_table_scoped(
    monkeypatch: pytest.MonkeyPatch, wiring: tuple[_FakeCtx, _FakeEngine]
) -> None:
    """The checkpoint name is scoped to the OMOP table being built."""
    ctx, engine = wiring
    monkeypatch.setattr(
        _common, "_read", lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError())
    )
    _common.build_common(_FakeDF(), ctx, table_name="observation")
    assert engine.checkpoint_calls[0][1] == "common_observation"


@pytest.mark.unit
def test_build_common_default_table_name(
    monkeypatch: pytest.MonkeyPatch, wiring: tuple[_FakeCtx, _FakeEngine]
) -> None:
    """Omitting ``table_name`` falls back to a generic ``common`` scope."""
    ctx, engine = wiring
    monkeypatch.setattr(
        _common, "_read", lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError())
    )
    _common.build_common(_FakeDF(), ctx)
    assert engine.checkpoint_calls[0][1] == "common_common"
