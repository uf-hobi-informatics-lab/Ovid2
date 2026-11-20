"""Unit tests — ``ovid2.packs.repair.build_repair_context``."""

from __future__ import annotations

from typing import Any

import pytest

from ovid2.packs.repair import build_repair_context

pytestmark = pytest.mark.unit


def _metric(table: str, flag: int, **extra: Any) -> dict[str, Any]:
    return {
        "source_table": table,
        "metric": "M",
        "value_num": 5.0,
        "threshold_num": 1.0,
        "exception_flag": flag,
        "value_str": "SECRET-ROW-VALUE",
        "details": "patid=P1",
    } | extra


_REPORT: dict[str, Any] = {
    "instance_id": "20260101-000000-abcdef",
    "pipeline": "demo",
    "batch_id": "acct/in",
    "cdm": "pcornet_7_0",
    "checks": [
        {"id": "ok", "status": "passed", "metrics": []},
        {
            "id": "w1",
            "name": "W1",
            "status": "exception",
            "exception_count": 3,
            "metrics": [_metric("A", 1), _metric("A", 0)],
        },
        {"id": "w2", "status": "exception", "exception_count": 9, "metrics": [_metric("B", 1)]},
        {"id": "b1", "status": "exception", "severity": "blocker", "exception_count": 1},
        {"id": "e1", "status": "error", "error": "boom", "exception_count": 0},
        {"id": "na", "status": "not_applicable"},
    ],
}

_CATALOGUE: dict[str, dict[str, Any]] = {
    "w1": {
        "severity": "warning",
        "pack": "pcornet_7_0",
        "remediation": {"hint": "do x", "fixers": ["race_eth"], "transforms": ["to_upper"]},
    },
    "w2": {"severity": "warning"},
    "e1": {"severity": "info", "description": "from catalogue"},
}


def test_failures_only_ordered_by_severity_then_count() -> None:
    ctx = build_repair_context(_REPORT, _CATALOGUE)
    assert [f["check_id"] for f in ctx["failures"]] == ["b1", "w2", "w1", "e1"]
    assert ctx["summary"] == {
        "failures": 4,
        "by_severity": {"blocker": 1, "warning": 2, "info": 1},
        "with_remediation": 1,
    }
    assert ctx["instance_id"] == _REPORT["instance_id"] and ctx["cdm"] == "pcornet_7_0"


def test_remediation_and_catalogue_fallbacks() -> None:
    by_id = {f["check_id"]: f for f in build_repair_context(_REPORT, _CATALOGUE)["failures"]}
    assert by_id["w1"]["remediation"] == {
        "hint": "do x",
        "fixers": ["race_eth"],
        "transforms": ["to_upper"],
        "docs": "",
    }
    assert by_id["w1"]["pack"] == "pcornet_7_0"
    assert by_id["e1"]["error"] == "boom" and by_id["e1"]["description"] == "from catalogue"
    assert by_id["w2"]["error"] is None
    assert len(by_id["w1"]["metrics"]) == 1  # non-exception metric dropped


def test_row_values_are_dropped_by_default() -> None:
    ctx = build_repair_context(_REPORT, _CATALOGUE)
    assert ctx["row_values_included"] is False
    assert "SECRET-ROW-VALUE" not in repr(ctx) and "patid=P1" not in repr(ctx)
    full = build_repair_context(_REPORT, _CATALOGUE, include_row_values=True)
    assert full["row_values_included"] is True
    assert "SECRET-ROW-VALUE" in repr(full)


def test_max_metrics_cap() -> None:
    report = {"checks": [{"id": "x", "status": "exception", "metrics": [_metric("T", 1)] * 50}]}
    ctx = build_repair_context(report, {}, max_metrics=5)
    assert len(ctx["failures"][0]["metrics"]) == 5
    assert ctx["failures"][0]["severity"] == "warning"  # default
