"""Unit tests for :class:`ovid2.scheduler.service.SchedulerService`.

Does not require Spark: uses stub engines and stub run helpers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ovid2.scheduler.batch_tracker import (
    STATUS_ERROR,
    STATUS_PROCESSED,
    SqliteBatchTracker,
)
from ovid2.scheduler.service import SchedulerService


class _StubEngine:
    """Engine that never really starts Spark — the tests short-circuit run_etl_job."""

    def get_session(self) -> object:
        return object()

    def stop_session(self) -> None:
        pass


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create ``tmp_path`` with a couple of pipeline batch directories.

    Layout::

        tmp_path/
          testpipeline/
            20260101/
            20260202/
          unknown_dir/       # not a known pipeline — must be ignored
    """
    root = tmp_path / "inbox"
    (root / "testpipeline" / "20260101").mkdir(parents=True)
    (root / "testpipeline" / "20260202").mkdir(parents=True)
    (root / "unknown_dir" / "20260101").mkdir(parents=True)
    return root


@pytest.fixture
def service(tmp_path: Path, data_dir: Path) -> SchedulerService:
    """Return a scheduler wired to a SQLite tracker + stub engine."""
    tracker = SqliteBatchTracker(tmp_path / "scheduler.db")
    tracker.initialize_tables()
    return SchedulerService(
        engine=_StubEngine(),
        input_address_name="disc_input",
        output_address_name="disc_output",
        poll_interval=1,
        min_wait=0,
        max_workers=1,
        data_path=data_dir,
        tracker=tracker,
    )


@pytest.mark.unit
def test_scan_matches_known_pipelines(service: SchedulerService) -> None:
    """Only directories matching a known pipeline are yielded as batches."""
    found = service.scan_disc_for_batches()
    ids = sorted(b["batch_id"] for b in found)
    assert ids == ["testpipeline/20260101", "testpipeline/20260202"]


@pytest.mark.unit
def test_pending_batches_register_and_return_all(service: SchedulerService) -> None:
    """First poll registers everything on disk and returns it (min_wait=0)."""
    pending = service.pending_batches()
    ids = sorted(b["batch_id"] for b in pending)
    assert ids == ["testpipeline/20260101", "testpipeline/20260202"]


@pytest.mark.unit
def test_pending_batches_skip_already_processed(service: SchedulerService) -> None:
    """Batches marked as PROCESSED stop appearing in the pending set."""
    service.pending_batches()  # register both
    service._tracker.update_status("testpipeline/20260101", STATUS_PROCESSED)  # type: ignore[union-attr]

    pending = service.pending_batches()
    ids = sorted(b["batch_id"] for b in pending)
    assert ids == ["testpipeline/20260202"]


@pytest.mark.unit
def test_run_etl_job_marks_tracker_transitions(
    service: SchedulerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful ETL run drives the batch NEW → PROCESSING → PROCESSED."""
    service.pending_batches()  # register batches

    # Force run_etl_job through a stub pipeline that always succeeds.
    def _fake_run_pipeline(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr("ovid2.core.pipeline.run_pipeline", _fake_run_pipeline, raising=True)
    monkeypatch.setattr(
        "ovid2.core.loaders.get_all_tables", lambda pipeline: ["demographic"], raising=True
    )
    monkeypatch.setattr(
        "ovid2.core.loaders.load_address",
        lambda *args, **kwargs: object(),
        raising=True,
    )
    monkeypatch.setattr(
        "ovid2.core.loaders.load_formatter",
        lambda *args, **kwargs: object(),
        raising=True,
    )
    monkeypatch.setattr(
        "ovid2.core.loaders.load_transformations",
        lambda names: [],
        raising=True,
    )

    ok = service.run_etl_job("testpipeline", "testpipeline/20260101")
    assert ok is True
    assert service._tracker.get_batch_status("testpipeline/20260101") == STATUS_PROCESSED  # type: ignore[union-attr]


@pytest.mark.unit
def test_run_etl_job_marks_error_on_failure(
    service: SchedulerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising pipeline drives the batch to ERROR."""
    service.pending_batches()  # register

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("ovid2.core.pipeline.run_pipeline", _boom, raising=True)
    monkeypatch.setattr(
        "ovid2.core.loaders.get_all_tables", lambda pipeline: ["demographic"], raising=True
    )
    monkeypatch.setattr(
        "ovid2.core.loaders.load_address",
        lambda *args, **kwargs: object(),
        raising=True,
    )
    monkeypatch.setattr(
        "ovid2.core.loaders.load_formatter",
        lambda *args, **kwargs: object(),
        raising=True,
    )
    monkeypatch.setattr(
        "ovid2.core.loaders.load_transformations",
        lambda names: [],
        raising=True,
    )

    ok = service.run_etl_job("testpipeline", "testpipeline/20260101")
    assert ok is False
    assert service._tracker.get_batch_status("testpipeline/20260101") == STATUS_ERROR  # type: ignore[union-attr]
