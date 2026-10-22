"""Phase 2 — `cdm` → `etl` kind rename + back-compat alias.

The historical ``cdm`` pipeline kind was renamed to ``etl`` in the
pipeline-centric panel refactor. ``cdm`` remains an accepted input
alias (normalised to ``etl`` at every validation entry point) so
existing on-disk route JSONs and external callers keep working.
"""

from __future__ import annotations

import pytest

from ovid2.web.models import KIND_ALIASES, PIPELINE_KINDS, normalize_kind


@pytest.mark.unit
def test_route_kinds_uses_etl_not_cdm() -> None:
    assert "etl" in PIPELINE_KINDS
    assert "cdm" not in PIPELINE_KINDS


@pytest.mark.unit
def test_cdm_alias_maps_to_etl() -> None:
    assert KIND_ALIASES["cdm"] == "etl"
    assert normalize_kind("cdm") == "etl"
    assert normalize_kind("CDM") == "etl"
    assert normalize_kind(" cdm ") == "etl"


@pytest.mark.unit
def test_normalize_kind_defaults_to_etl() -> None:
    assert normalize_kind(None) == "etl"
    assert normalize_kind("") == "etl"
    assert normalize_kind("   ") == "etl"


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["etl", "analysis", "dq_checks", "copy", "move"])
def test_canonical_kinds_pass_through(kind: str) -> None:
    assert normalize_kind(kind) == kind


@pytest.mark.unit
def test_worker_dispatches_cdm_alias_to_etl_subcommand() -> None:
    """A persisted job with the legacy ``cdm`` kind still runs ``ovid2 etl``."""
    from ovid2.web.models import JobDetail
    from ovid2.web.worker import Worker

    job = JobDetail(
        job_id="J1",
        instance_id="I1",
        submitted_at="2026-01-01T00:00:00Z",
        submitted_by="test",
        parent_job_id=None,
        pipeline="demo",
        batch_id="B1",
        input_address="in",
        output_address="out",
        tables=["demographic"],
        transformations=["all"],
        extra_args={"kind": "cdm", "cdm": "pcornet_7_0", "engine": "pyspark", "workers": 25},
        status="queued",
        cancel_requested=False,
    )
    worker = Worker.__new__(Worker)
    worker.ovid2_bin = ["python", "-m", "ovid2.cli"]
    argv = worker._build_argv(job)
    assert "etl" in argv
    # `--cdm` is still emitted for the etl kind.
    assert "--cdm" in argv
    assert "pcornet_7_0" in argv
