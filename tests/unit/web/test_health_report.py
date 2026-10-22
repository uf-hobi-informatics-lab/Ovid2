"""Unit tests for the data-health web surface (report svc, routes, worker)."""

from __future__ import annotations

import importlib
import json
from pathlib import Path


def _report_dict(instance_id: str) -> dict:
    return {
        "v": 1,
        "instance_id": instance_id,
        "pipeline": "demo",
        "batch_id": "b1",
        "cdm": "pcornet_7_0",
        "input_address": "IN",
        "output_address": "OUT",
        "datamart_id": None,
        "refresh_date": None,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "results_uri": "OUT:demo/b1/dcq_results",
        "summary": {
            "checks_total": 1,
            "checks_passed": 0,
            "checks_exception": 1,
            "checks_error": 0,
            "checks_skipped": 0,
            "parts": {"part1": {"total": 1, "passed": 0, "exception": 1, "error": 0}},
        },
        "checks": [
            {
                "id": "1.01",
                "name": "DC_1_01",
                "part": "part1",
                "edc_table": "Table IID",
                "description": "Required tables present",
                "status": "exception",
                "exception_count": 1,
                "metric_count": 1,
                "error": None,
                "doc_interpretation": "missing required table",
                "metrics": [
                    {
                        "source_table": "HARVEST",
                        "metric": "MISSING_REQUIRED_TABLE_FLAG",
                        "value_num": 1.0,
                        "value_str": None,
                        "threshold_num": 1.0,
                        "exception_flag": True,
                        "details": {"required": True},
                    },
                ],
            },
        ],
    }


def _work_dir() -> Path:
    import ovid2.config.settings as settings

    return settings.WORK_DIR


def _write_report(instance_id: str) -> None:
    work = _work_dir()
    work.mkdir(parents=True, exist_ok=True)
    (work / f"health-report-{instance_id}.json").write_text(json.dumps(_report_dict(instance_id)))


# --------------------------------------------------------------------------- #
# health_report_svc                                                            #
# --------------------------------------------------------------------------- #


def test_report_available_and_get(isolated_xdg) -> None:
    from ovid2.web.services import health_report_svc

    importlib.reload(health_report_svc)
    assert health_report_svc.report_available("nope") is False
    _write_report("20260101-000000-abcdef")
    assert health_report_svc.report_available("20260101-000000-abcdef") is True
    report = health_report_svc.get_report("20260101-000000-abcdef")
    assert report is not None and report["summary"]["checks_exception"] == 1


def test_get_report_missing_returns_none(isolated_xdg) -> None:
    from ovid2.web.services import health_report_svc

    importlib.reload(health_report_svc)
    assert health_report_svc.get_report("missing") is None


def test_list_checks_web_safe() -> None:
    from ovid2.web.services import health_report_svc

    checks = health_report_svc.list_checks()
    assert checks
    ids = {c["id"] for c in checks}
    assert "1.01" in ids
    assert all("operator" in c for c in checks)


def test_list_checks_canonical_order_matches_runner() -> None:
    """The web catalogue must list checks in the runner's canonical order.

    Regression guard for the "checks listed out of alphabetical order"
    bug: the catalogue used to sort by the part *string*, which floats
    ``completeness``/``freshness``/``integrity``/``mapping`` above
    ``part1..part4``. It must instead follow the numeric part bucket
    order (``part1 → … → part4 → mapping → …``) with a natural id sort,
    matching :attr:`CheckSpec.sort_key`.
    """
    from ovid2.transformations.quality.registry import load_all_specs
    from ovid2.web.services import health_report_svc

    web_ids = [c["id"] for c in health_report_svc.list_checks()]
    registry_ids = [s.id for s in load_all_specs()]
    assert web_ids == registry_ids

    parts_in_order = [c["part"] for c in health_report_svc.list_checks()]
    # part4 must precede the non-part buckets (the old string sort put
    # them first).
    assert parts_in_order.index("part4") < parts_in_order.index("mapping")


def test_natural_id_key_sorts_numerically() -> None:
    """``1.2`` must sort before ``1.10`` (natural, not string, order)."""
    from ovid2.web.services.health_report_svc import _natural_id_key

    assert _natural_id_key("1.2") < _natural_id_key("1.10")
    assert _natural_id_key("1.01") < _natural_id_key("2.01")


# --------------------------------------------------------------------------- #
# HTTP surface                                                                 #
# --------------------------------------------------------------------------- #


def test_api_health_checks(web_client) -> None:
    r = web_client.get("/api/v1/health/checks")
    assert r.status_code == 200
    assert any(x["id"] == "1.01" for x in r.json())


def test_api_report_404_when_absent(web_client) -> None:
    assert web_client.get("/api/v1/runs/nope/report").status_code == 404


def test_api_report_200(web_client) -> None:
    _write_report("20260101-000000-abcdef")
    r = web_client.get("/api/v1/runs/20260101-000000-abcdef/report")
    assert r.status_code == 200
    assert r.json()["summary"]["checks_exception"] == 1


def test_report_viewer_html(web_client) -> None:
    _write_report("20260101-000000-abcdef")
    r = web_client.get("/runs/20260101-000000-abcdef/report")
    assert r.status_code == 200
    for needle in ("Data-health report", "DC_1_01", "MISSING_REQUIRED_TABLE_FLAG"):
        assert needle in r.text


def test_report_viewer_404(web_client) -> None:
    assert web_client.get("/runs/missing/report").status_code == 404


# --------------------------------------------------------------------------- #
# Worker argv                                                                  #
# --------------------------------------------------------------------------- #


def test_worker_builds_health_argv() -> None:
    from ovid2.web.models import JobDetail
    from ovid2.web.worker import Worker

    job = JobDetail(
        job_id="j1",
        submitted_at="2026-01-01T00:00:00Z",
        submitted_by="test",
        pipeline="demo",
        batch_id="b1",
        input_address="IN",
        output_address="OUT",
        tables=[],
        transformations=[],
        status="queued",
        extra_args={
            "kind": "health",
            "parts": ["part1", "integrity"],
            "checks": ["1.01"],
            "prev_input_address": "PREV",
            "engine": "pyspark",
        },
    )
    w = Worker.__new__(Worker)
    w.ovid2_bin = ["ovid2"]
    argv = w._build_argv(job)
    assert argv[:2] == ["ovid2", "health"]
    assert "--parts" in argv and "part1" in argv and "integrity" in argv
    assert "--checks" in argv and "1.01" in argv
    assert "--prev_input_address" in argv and "PREV" in argv
    assert "--transformations" not in argv
