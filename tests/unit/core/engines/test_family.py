"""Unit tests for :mod:`ovid2.core.engines.family` — pipeline ↔ engine gating."""

from __future__ import annotations

import sys
import types

import pytest

from ovid2.core.engines import EngineFamily
from ovid2.core.engines.family import (
    assert_pipeline_engine_compatible,
    resolve_pipeline_family,
)


class _FakeEngine:
    """Minimal stand-in that exposes just what the gate looks at."""

    def __init__(self, family: EngineFamily, type_name: str = "fake") -> None:
        self.family = family
        self.type_name = type_name


def _install_pipeline_settings(monkeypatch: pytest.MonkeyPatch, name: str, **attrs) -> None:
    """Register a synthetic ``ovid2_pipelines.<name>.pipeline_settings`` module."""
    pkg_name = f"ovid2_pipelines.{name}"
    settings_name = f"{pkg_name}.pipeline_settings"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = []  # marks as a namespace package
    settings = types.ModuleType(settings_name)
    for k, v in attrs.items():
        setattr(settings, k, v)
    monkeypatch.setitem(sys.modules, pkg_name, pkg)
    monkeypatch.setitem(sys.modules, settings_name, settings)


@pytest.mark.unit
def test_resolve_pipeline_family_returns_none_when_settings_missing() -> None:
    """No pipeline_settings module means None (portable)."""
    assert resolve_pipeline_family("bogus_pipeline_that_does_not_exist") is None


@pytest.mark.unit
def test_resolve_pipeline_family_returns_none_when_marker_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pipeline without REQUIRES_FAMILY is portable."""
    _install_pipeline_settings(monkeypatch, "portable_pipeline")
    assert resolve_pipeline_family("portable_pipeline") is None


@pytest.mark.unit
def test_resolve_pipeline_family_reads_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit REQUIRES_FAMILY is returned lower-cased."""
    _install_pipeline_settings(monkeypatch, "spark_only_pipeline", REQUIRES_FAMILY="SPARK")
    assert resolve_pipeline_family("spark_only_pipeline") == "spark"


@pytest.mark.unit
def test_gate_passes_when_engine_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spark pipeline + Spark engine → no exception."""
    _install_pipeline_settings(monkeypatch, "spark_only_a", REQUIRES_FAMILY="spark")
    assert_pipeline_engine_compatible(_FakeEngine(EngineFamily.SPARK), "spark_only_a")


@pytest.mark.unit
def test_gate_passes_when_pipeline_is_portable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Portable pipeline + Snowpark engine → no exception."""
    _install_pipeline_settings(monkeypatch, "portable_a")
    assert_pipeline_engine_compatible(_FakeEngine(EngineFamily.SNOWPARK), "portable_a")


@pytest.mark.unit
def test_gate_raises_on_family_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spark-only pipeline + Snowpark engine → RuntimeError with a clear message."""
    _install_pipeline_settings(monkeypatch, "spark_only_b", REQUIRES_FAMILY="spark")
    engine = _FakeEngine(EngineFamily.SNOWPARK, type_name="snowpark_prod")
    with pytest.raises(RuntimeError) as exc:
        assert_pipeline_engine_compatible(engine, "spark_only_b")
    msg = str(exc.value)
    assert "spark_only_b" in msg
    assert "spark" in msg
    assert "snowpark" in msg
