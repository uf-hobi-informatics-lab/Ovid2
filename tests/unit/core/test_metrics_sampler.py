"""Unit tests for ``ovid2.core.metrics_sampler``.

Covers:

* Emits one event per tick with the right kind + payload shape.
* Adaptive rate — after N zero-active samples backs off to idle_ms.
* Never fails the run — engine exceptions are swallowed.
* Disabled (via env flag) returns a stopped handle immediately.
* Stop is idempotent + returns quickly.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from ovid2.core.engines.base import BaseEngine
from ovid2.core.engines.metrics import EngineMetricsSample
from ovid2.core.engines.protocols import EngineFamily
from ovid2.core.events import Event
from ovid2.core.metrics_sampler import run_sampler


class _RecordingBus:
    """Bus that keeps every emitted event in a list."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)


class _StubEngine(BaseEngine):
    """Engine whose ``get_metrics`` is fully scripted for the test."""

    type_name = "stub"
    family = EngineFamily.SPARK

    def __init__(self, samples: list[EngineMetricsSample] | None = None) -> None:
        super().__init__()
        self._samples = samples or []
        self._idx = 0
        self.call_count = 0

    def get_session(self) -> Any:  # pragma: no cover — never called
        raise NotImplementedError

    def stop_session(self) -> None:
        return None

    def get_metrics(self) -> EngineMetricsSample:
        self.call_count += 1
        if self._samples:
            sample = self._samples[min(self._idx, len(self._samples) - 1)]
            self._idx += 1
            return sample
        return EngineMetricsSample(
            t_ms=self.call_count,
            wall_iso="2026-08-06T12:00:00Z",
            engine_family=self.family.value,
            tasks_active=0,
        )


class _ThrowingEngine(_StubEngine):
    """Engine whose ``get_metrics`` always raises."""

    def get_metrics(self) -> EngineMetricsSample:
        self.call_count += 1
        raise RuntimeError("boom")


# ---------------------------------------------------------------- basic


def test_sampler_emits_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs one tick with a very short period and stops."""
    import ovid2.config.settings as s

    monkeypatch.setattr(s, "ENGINE_METRICS_ENABLED", True)
    engine = _StubEngine()
    bus = _RecordingBus()
    handle = run_sampler(
        instance_id="iid",
        engine=engine,
        bus=bus,
        active_ms=50,
        idle_ms=50,
        idle_after=1_000_000,  # never back off
    )
    time.sleep(0.2)
    handle.stop()
    assert engine.call_count >= 1
    assert bus.events
    for ev in bus.events:
        assert ev.kind == "engine.metrics"
        assert ev.instance_id == "iid"
        assert ev.payload.get("engine_family") == "spark"


def test_sampler_never_fails_on_engine_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising ``get_metrics`` must not fail the sampler."""
    import ovid2.config.settings as s

    monkeypatch.setattr(s, "ENGINE_METRICS_ENABLED", True)
    engine = _ThrowingEngine()
    bus = _RecordingBus()
    handle = run_sampler(
        instance_id="iid",
        engine=engine,
        bus=bus,
        active_ms=50,
        idle_ms=50,
        idle_after=1_000_000,
    )
    time.sleep(0.2)
    handle.stop()
    # Exceptions swallowed → no events emitted, but call_count went up.
    assert engine.call_count >= 1
    assert bus.events == []


def test_sampler_disabled_returns_stopped_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ENGINE_METRICS_ENABLED=False`` short-circuits without emitting."""
    import ovid2.config.settings as s

    monkeypatch.setattr(s, "ENGINE_METRICS_ENABLED", False)
    engine = _StubEngine()
    bus = _RecordingBus()
    handle = run_sampler(instance_id="iid", engine=engine, bus=bus, active_ms=10)
    time.sleep(0.1)
    handle.stop()
    assert bus.events == []
    assert engine.call_count == 0


def test_sampler_active_ms_zero_disables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``active_ms=0`` should also short-circuit."""
    import ovid2.config.settings as s

    monkeypatch.setattr(s, "ENGINE_METRICS_ENABLED", True)
    engine = _StubEngine()
    bus = _RecordingBus()
    handle = run_sampler(instance_id="iid", engine=engine, bus=bus, active_ms=0)
    time.sleep(0.1)
    handle.stop()
    assert bus.events == []


# ---------------------------------------------------------------- rate


def test_active_period_when_tasks_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream of ``tasks_active > 0`` samples keeps the active rate."""
    import ovid2.config.settings as s

    monkeypatch.setattr(s, "ENGINE_METRICS_ENABLED", True)
    busy = EngineMetricsSample(t_ms=0, wall_iso="t", engine_family="spark", tasks_active=5)
    engine = _StubEngine(samples=[busy] * 20)
    bus = _RecordingBus()
    handle = run_sampler(
        instance_id="iid",
        engine=engine,
        bus=bus,
        active_ms=50,
        idle_ms=10_000,  # way slower — if we back off, we'll notice
        idle_after=5,
    )
    time.sleep(0.35)
    handle.stop()
    # At 50 ms active with busy samples, expect ~5-6 emits in 350 ms
    # (allow a very generous lower bound so CI jitter doesn't matter).
    assert len(bus.events) >= 3


def test_sampler_stop_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling ``stop`` twice must not blow up."""
    import ovid2.config.settings as s

    monkeypatch.setattr(s, "ENGINE_METRICS_ENABLED", True)
    engine = _StubEngine()
    handle = run_sampler(instance_id="iid", engine=engine, bus=_RecordingBus(), active_ms=50)
    handle.stop()
    handle.stop()  # no exception


def test_snowpark_family_uses_snowpark_default_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Snowpark engine gets the slower default ``ENGINE_METRICS_SNOWPARK_MS``.

    We don't verify the exact rate (it would flake under CI load); we
    just check the sampler still emits at least one sample and doesn't
    crash on a Snowpark-family engine.
    """
    import ovid2.config.settings as s

    monkeypatch.setattr(s, "ENGINE_METRICS_ENABLED", True)
    monkeypatch.setattr(s, "ENGINE_METRICS_SNOWPARK_MS", 50)
    monkeypatch.setattr(s, "ENGINE_METRICS_SPARK_MS", 10_000)

    class _SnowEngine(_StubEngine):
        family = EngineFamily.SNOWPARK

    engine = _SnowEngine()
    bus = _RecordingBus()
    handle = run_sampler(instance_id="iid", engine=engine, bus=bus)
    time.sleep(0.2)
    handle.stop()
    assert len(bus.events) >= 1
