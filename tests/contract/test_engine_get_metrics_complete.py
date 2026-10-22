"""Contract: every registered engine exposes ``get_metrics``.

The metrics sampler polls ``engine.get_metrics()`` on a fixed cadence
while a run is in flight. Every engine class registered in
:data:`ovid2.core.engines.registry.ENGINE_TYPE_MAP` must implement the
method — either by overriding :meth:`BaseEngine.get_metrics` (Spark
engines do) or by inheriting the default "engine alive" stub
(Snowpark). This test locks in that presence so a new engine can't be
added without at least the base behaviour.
"""

from __future__ import annotations

import pytest

from ovid2.core.engines.metrics import EngineMetricsSample
from ovid2.core.engines.registry import ENGINE_TYPE_MAP, load_engine_class


@pytest.mark.contract
def test_every_engine_implements_get_metrics() -> None:
    """Every ``ENGINE_TYPE_MAP`` entry has a callable ``get_metrics``."""
    for type_name in sorted(ENGINE_TYPE_MAP):
        cls = load_engine_class(type_name)
        assert hasattr(cls, "get_metrics"), (
            f"Engine '{type_name}' ({cls.__module__}.{cls.__name__}) "
            f"has no get_metrics method"
        )
        assert callable(cls.get_metrics), (
            f"Engine '{type_name}' get_metrics is not callable"
        )


@pytest.mark.contract
def test_default_get_metrics_returns_bare_sample() -> None:
    """The base default emits a family-only sample without a session.

    Locks in the invariant that ``get_metrics`` is safe to call before
    the engine has done anything — the sampler starts before the first
    job.
    """
    from ovid2.core.engines.base import BaseEngine
    from ovid2.core.engines.protocols import EngineFamily

    class _StubEngine(BaseEngine):
        type_name = "stub"
        family = EngineFamily.SPARK

        def get_session(self):  # noqa: D401
            raise NotImplementedError

        def stop_session(self) -> None:
            return None

    sample = _StubEngine().get_metrics()
    assert isinstance(sample, EngineMetricsSample)
    assert sample.engine_family == "spark"
    assert sample.executors_active is None
    assert sample.tasks_active is None
