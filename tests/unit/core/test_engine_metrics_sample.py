"""Unit tests for :class:`ovid2.core.engines.metrics.EngineMetricsSample`.

Round-trip / payload-shaping tests. The dataclass itself is trivial;
what matters is that ``to_payload`` drops ``None`` fields so the
serialised event stays compact.
"""

from __future__ import annotations

from ovid2.core.engines.metrics import EngineMetricsSample


def test_payload_drops_none_fields() -> None:
    """Fields left at their default ``None`` do not appear in the payload."""
    sample = EngineMetricsSample(
        t_ms=1234,
        wall_iso="2026-08-06T12:00:00Z",
        engine_family="spark",
        tasks_active=3,
    )
    payload = sample.to_payload()
    assert payload["t_ms"] == 1234
    assert payload["wall_iso"] == "2026-08-06T12:00:00Z"
    assert payload["engine_family"] == "spark"
    assert payload["tasks_active"] == 3
    # No key for the ``None`` defaults.
    assert "executors_active" not in payload
    assert "current_stage" not in payload


def test_payload_drops_empty_custom_dict() -> None:
    """The default empty ``custom={}`` must not appear in the payload."""
    sample = EngineMetricsSample(t_ms=1, wall_iso="t", engine_family="spark")
    payload = sample.to_payload()
    assert "custom" not in payload


def test_payload_keeps_populated_custom_dict() -> None:
    """A non-empty ``custom`` survives the payload trim."""
    sample = EngineMetricsSample(
        t_ms=1, wall_iso="t", engine_family="spark", custom={"app_url": "http://x"}
    )
    payload = sample.to_payload()
    assert payload["custom"] == {"app_url": "http://x"}


def test_payload_keeps_zero_and_empty_string() -> None:
    """Zero and empty-string values are NOT stripped (they carry meaning)."""
    sample = EngineMetricsSample(
        t_ms=0,
        wall_iso="",
        engine_family="spark",
        tasks_active=0,
        current_stage="",
    )
    payload = sample.to_payload()
    assert payload["t_ms"] == 0
    assert payload["wall_iso"] == ""
    assert payload["tasks_active"] == 0
    assert payload["current_stage"] == ""
