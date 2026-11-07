"""``BaseEngine.checkpoint`` / ``release_checkpoints`` lifecycle."""

from __future__ import annotations

from typing import Any

import pytest

from ovid2.core.engines.base import BaseEngine

pytestmark = pytest.mark.unit


class _Engine(BaseEngine):
    type_name = "fake"

    def get_session(self) -> Any:  # pragma: no cover - unused
        return None

    def stop_session(self) -> None:  # pragma: no cover - unused
        return None


class _DF:
    def __init__(self) -> None:
        self.cached = False
        self.unpersisted: list[bool] = []

    def cache(self) -> _DF:
        self.cached = True
        return self

    def unpersist(self, blocking: bool = False) -> _DF:
        self.unpersisted.append(blocking)
        return self


def test_checkpoint_caches_and_release_unpersists() -> None:
    engine = _Engine()
    a, b = _DF(), _DF()
    assert engine.checkpoint(a, "a") is a and a.cached
    engine.checkpoint(b, "b")
    assert engine.release_checkpoints() == 2
    assert a.unpersisted == [False] and b.unpersisted == [False]
    assert engine.release_checkpoints() == 0


def test_release_is_best_effort() -> None:
    class _Boom(_DF):
        def unpersist(self, blocking: bool = False) -> _DF:
            raise RuntimeError("jvm gone")

    engine = _Engine()
    engine.checkpoint(_Boom(), "x")
    engine.checkpoint(_DF(), "y")
    assert engine.release_checkpoints() == 1


@pytest.mark.integration
def test_spark_checkpoint_storage_freed(spark: Any) -> None:
    from ovid2.core.engines.pyspark import PySparkEngine

    engine = PySparkEngine()
    engine._session = spark
    df = engine.checkpoint(spark.range(100), "t")
    df.count()
    assert df.is_cached
    engine.release_checkpoints()
    assert not df.is_cached
