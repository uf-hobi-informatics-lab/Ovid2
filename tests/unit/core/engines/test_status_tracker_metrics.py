"""Regression tests for the Spark ``StatusTracker`` metrics reader.

``pyspark.status.SparkStageInfo`` is a namedtuple: its counters are
*fields*. The sampler used to call them (``info.numActiveTasks()``),
which raised ``TypeError`` inside a broad ``except`` and silently turned
every live sample into a bare one. These tests pin the field access and
check it against a real job in a local Spark session.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from ovid2.core.engines.pyspark import read_status_tracker


class _FakeTracker:
    def __init__(self, stages: dict[int, Any]) -> None:
        self._stages = stages

    def getActiveStageIds(self) -> list[int]:
        return list(self._stages)

    def getStageInfo(self, sid: int) -> Any:
        return self._stages[sid]


def _stage(name: str, active: int, done: int, failed: int) -> Any:
    from pyspark.status import SparkStageInfo

    return SparkStageInfo(
        stageId=0,
        currentAttemptId=0,
        name=name,
        numTasks=active + done + failed,
        numActiveTasks=active,
        numCompletedTasks=done,
        numFailedTasks=failed,
    )


@pytest.mark.unit
def test_reads_namedtuple_fields_and_degrades_without_jvm() -> None:
    pytest.importorskip("pyspark")
    tracker = _FakeTracker({1: _stage("a", 2, 3, 0), 2: _stage("b", 1, 4, 1), 3: None})
    sc = SimpleNamespace(statusTracker=lambda: tracker)  # no ``_jsc``

    snap = read_status_tracker(sc)

    assert snap.active_stage_ids == [1, 2, 3]
    assert (snap.tasks_active, snap.tasks_completed, snap.tasks_failed) == (3, 7, 1)
    assert snap.current_stage == "b"
    assert snap.executors_total is None
    assert snap.executors_active is None


@pytest.mark.unit
def test_live_sample_reports_running_tasks(spark: Any) -> None:
    """A sample taken mid-job carries task counts, not a bare sample."""
    from ovid2.core.engines.pyspark import PySparkEngine

    engine = PySparkEngine()
    engine._session = spark  # reuse the session-scoped fixture

    def job() -> None:
        spark.range(0, 40_000_000, numPartitions=16).selectExpr("sum(id * id)").collect()  # noqa: OVID2001

    worker = threading.Thread(target=job)
    worker.start()
    try:
        sample = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            sample = engine.get_metrics()
            if sample.stages_total:
                break
            time.sleep(0.05)
    finally:
        worker.join()

    assert sample is not None and sample.stages_total, "never observed an active stage"
    assert sample.tasks_active is not None
    assert sample.executors_total is not None and sample.executors_total >= 1
