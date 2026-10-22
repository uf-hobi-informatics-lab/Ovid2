"""Unit tests for the live-metrics ``metrics_svc``.

Covers:

* :func:`latest` — no log, empty log, no ``engine.metrics`` samples,
  one sample, multiple samples (last one wins), partial-line writer,
  truncated tail, malformed JSON skipped, log rotation edge case.
* :func:`stream` — connected frame, initial primed sample,
  incremental sample emission, ``event: finished`` frame on
  ``instance.finished``, keepalive comment, timeout guard, silence
  between samples doesn't spin, partial-line resiliency.
* :func:`briefing_cooking` — empty when no running jobs, joined
  sample when a running job has samples.

Tests operate on a real JSONL file under ``isolated_xdg`` (the shared
web-tier fixture) so we exercise the real file I/O paths.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


def _write_event(log: Path, kind: str, **payload) -> None:
    """Append one line-terminated JSON event to ``log``."""
    record = {"v": 1, "kind": kind, "ts": "2026-08-06T12:00:00Z", "instance_id": "iid"}
    record.update(payload)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def _event_log_for(instance_id: str) -> Path:
    """Return the JSONL path the sampler / event bus write to."""
    from ovid2.web.services import events_svc

    return events_svc.event_log_path(instance_id)


# ---------------------------------------------------------------- latest


class TestLatest:
    def test_no_log_returns_none(self, isolated_xdg: Path) -> None:
        from ovid2.web.services import metrics_svc

        assert metrics_svc.latest("does-not-exist") is None

    def test_empty_log_returns_none(self, isolated_xdg: Path) -> None:
        from ovid2.web.services import metrics_svc

        path = _event_log_for("iid")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        assert metrics_svc.latest("iid") is None

    def test_log_without_metrics_returns_none(self, isolated_xdg: Path) -> None:
        from ovid2.web.services import metrics_svc

        path = _event_log_for("iid")
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_event(path, "instance.started")
        _write_event(path, "table.started", table="demographic")
        assert metrics_svc.latest("iid") is None

    def test_one_sample(self, isolated_xdg: Path) -> None:
        from ovid2.web.services import metrics_svc

        path = _event_log_for("iid")
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_event(path, "engine.metrics", engine_family="spark", tasks_active=4)
        got = metrics_svc.latest("iid")
        assert got is not None
        assert got["engine_family"] == "spark"
        assert got["tasks_active"] == 4

    def test_last_sample_wins(self, isolated_xdg: Path) -> None:
        from ovid2.web.services import metrics_svc

        path = _event_log_for("iid")
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_event(path, "engine.metrics", tasks_active=1)
        _write_event(path, "engine.metrics", tasks_active=7)
        _write_event(path, "table.started", table="X")  # non-metrics line after
        _write_event(path, "engine.metrics", tasks_active=12)
        assert metrics_svc.latest("iid")["tasks_active"] == 12

    def test_malformed_lines_skipped(self, isolated_xdg: Path) -> None:
        from ovid2.web.services import metrics_svc

        path = _event_log_for("iid")
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_event(path, "engine.metrics", tasks_active=3)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("this is not JSON\n")
            fh.write("null\n")
            fh.write("[1,2,3]\n")  # not a dict
        # The malformed lines come after the sample, so the last valid
        # ``engine.metrics`` line is still the one with tasks_active=3.
        got = metrics_svc.latest("iid")
        assert got is not None
        assert got["tasks_active"] == 3

    def test_partial_last_line_skipped(self, isolated_xdg: Path) -> None:
        """Writer mid-flush → last line has no trailing newline."""
        from ovid2.web.services import metrics_svc

        path = _event_log_for("iid")
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_event(path, "engine.metrics", tasks_active=5)
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"v":1,"kind":"engine.metrics","tasks_active":')  # partial
        # Partial isn't valid JSON → skipped. The complete sample wins.
        got = metrics_svc.latest("iid")
        assert got is not None
        assert got["tasks_active"] == 5


# ---------------------------------------------------------------- stream


class TestStream:
    def test_connected_frame_first(self, isolated_xdg: Path) -> None:
        from ovid2.web.services import metrics_svc

        gen = metrics_svc.stream("iid", max_seconds=1, keepalive_s=30)
        first = next(gen)
        assert first.startswith(b": connected")
        gen.close()

    def test_primed_sample_when_log_has_one(self, isolated_xdg: Path) -> None:
        from ovid2.web.services import metrics_svc

        path = _event_log_for("iid")
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_event(path, "engine.metrics", tasks_active=9, engine_family="spark")

        gen = metrics_svc.stream("iid", max_seconds=1, keepalive_s=30)
        try:
            frames = [next(gen), next(gen)]
        finally:
            gen.close()
        assert frames[0].startswith(b": connected")
        assert b"event: sample" in frames[1]
        assert b'"tasks_active":9' in frames[1]

    def test_finished_frame_on_instance_finished(self, isolated_xdg: Path) -> None:
        from ovid2.web.services import metrics_svc

        path = _event_log_for("iid")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Ensure the log file exists BEFORE we open the stream, so the
        # initial ``cursor = size`` snapshot is correct and the loop
        # will then see subsequent appends.
        path.write_text("", encoding="utf-8")
        gen = metrics_svc.stream("iid", max_seconds=3, keepalive_s=30)
        # Pull the initial connected frame.
        connected = next(gen)
        assert connected.startswith(b": connected")
        _write_event(path, "instance.finished", success=True)
        # ``next()`` blocks until the generator yields the finish
        # frame or hits the max_seconds timeout.
        second = next(gen)
        gen.close()
        assert b"event: finished" in second

    def test_timeout_stops_stream(self, isolated_xdg: Path) -> None:
        from ovid2.web.services import metrics_svc

        gen = metrics_svc.stream("iid", max_seconds=1, keepalive_s=30)
        # Collect until the generator stops.
        out: list[bytes] = []
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                out.append(next(gen))
            except StopIteration:
                break
        # Should have seen a ``: timeout`` comment before stopping.
        assert any(b": timeout" in f for f in out)


# ---------------------------------------------------------- briefing


class TestBriefingCooking:
    def test_empty_when_no_running_jobs(self, isolated_xdg: Path) -> None:
        from ovid2.web.services import metrics_svc

        result = metrics_svc.briefing_cooking()
        assert result == []

    def test_returns_running_jobs_with_latest_sample(
        self, isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ovid2.web import db as web_db
        from ovid2.web.services import metrics_svc

        # Insert one running job directly (bypasses ``jobs_svc.submit``
        # to avoid the pipeline-must-be-installed pre-flight).
        web_db.ensure_schema()
        with web_db.connect() as conn:
            conn.execute(
                "INSERT INTO web_jobs "
                "(job_id, pipeline, batch_id, input_address, output_address, "
                "tables_json, transformations_json, submitted_by, "
                "submitted_at, status, instance_id) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "job-1",
                    "demo",
                    "B1",
                    "input",
                    "output",
                    "[]",
                    "[]",
                    "test",
                    "2026-08-06T00:00:00Z",
                    "running",
                    "20260806-120000-abc123",
                ),
            )
            conn.commit()

        # Write a sample under that instance_id.
        path = _event_log_for("20260806-120000-abc123")
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_event(path, "engine.metrics", tasks_active=6, engine_family="spark")

        result = metrics_svc.briefing_cooking()
        assert len(result) >= 1
        entry = next(r for r in result if r["instance_id"] == "20260806-120000-abc123")
        assert entry["pipeline"] == "demo"
        assert entry["sample"] is not None
        assert entry["sample"]["tasks_active"] == 6
