"""Web tier accepts every engine for every pipeline package (engine universality).

History: an operator (via an assistant-drafted job) launched the
``legacy_ovid`` port ``acme`` on ``--engine duckdb``. The engine-family gate
refused it ("Pipeline 'acme' needs a 'spark' engine"). Every engine now runs
every pipeline (``adr-engine-universality.md``), so submit / draft / launch
/ rerun must accept the pairing — and the gate must not come back.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from ovid2.web import db as web_db
from ovid2.web.models import JobSubmitRequest
from ovid2.web.services import jobs_svc


@pytest.fixture()
def legacy_pkg(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str]:
    """A stub ``legacy_ovid`` package plus a disc input / output address."""
    root = tmp_path / "pipelines_root"
    pkg = root / "ovid2_pipelines" / "tp_legacy_port"
    (pkg / "formatters").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "pipeline_settings.py").write_text(
        'mappers_type = "pcornet_mappers"\nETL_PROFILE = "legacy_ovid"\n'
        'REQUIRES_FAMILY = "spark"\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(root))
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]
    data = tmp_path / "data"
    (data / "in").mkdir(parents=True)
    conns = Path(os.environ["OVID2_CONNECTIONS_DIR"])
    (conns / "box.json").write_text(
        json.dumps({"type": "disc", "args": {"host_path": str(data)}}), encoding="utf-8"
    )
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    (configs / "lp_in.json").write_text(
        json.dumps({"connection": "box", "location": {"path": "in"}}), encoding="utf-8"
    )
    (configs / "lp_out.json").write_text(
        json.dumps({"connection": "box", "location": {"path": "out"}}), encoding="utf-8"
    )
    yield "tp_legacy_port"
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]


def _req(pipeline: str, engine: str) -> JobSubmitRequest:
    return JobSubmitRequest(
        pipeline=pipeline,
        batch_id="x",
        input_address="lp_in",
        output_address="lp_out",
        engine=engine,
    )


@pytest.mark.unit
@pytest.mark.parametrize("engine", ["duckdb", "pyspark", "snowpark", "databricks_spark"])
@pytest.mark.parametrize("entry", [jobs_svc.submit, jobs_svc.create_draft])
def test_submit_and_draft_accept_every_engine_for_legacy_port(
    legacy_pkg: str, entry: object, engine: str
) -> None:
    """``POST /runs`` / ``POST /job-drafts`` accept a legacy port on any engine.

    Even a stale ``REQUIRES_FAMILY`` marker in the package is ignored. The
    disc storage is reachable from every engine, so the only gate that
    could refuse is the (removed) family gate.
    """
    resp = entry(_req(legacy_pkg, engine))  # type: ignore[operator]
    assert resp.job_id


def _force_engine(job_id: str, engine: str, status: str) -> None:
    """Rewrite a stored row's engine / status."""
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT extra_args_json FROM web_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        extra = json.loads(row["extra_args_json"])
        extra["engine"] = engine
        conn.execute(
            "UPDATE web_jobs SET extra_args_json = ?, status = ? WHERE job_id = ?",
            (json.dumps(extra), status, job_id),
        )


@pytest.mark.unit
def test_launch_accepts_a_duckdb_draft_of_a_legacy_port(legacy_pkg: str) -> None:
    """A duckdb draft of a legacy port launches (queued, not refused)."""
    resp = jobs_svc.create_draft(_req(legacy_pkg, "duckdb"))
    jobs_svc.launch_draft(resp.job_id)
    detail = jobs_svc.get_job(resp.job_id)
    assert detail is not None and detail.status != jobs_svc.STATUS_DRAFT


@pytest.mark.unit
def test_rerun_accepts_a_duckdb_job_of_a_legacy_port(legacy_pkg: str) -> None:
    """Rerunning a finished duckdb job of a legacy port is accepted."""
    resp = jobs_svc.submit(_req(legacy_pkg, "pyspark"))
    _force_engine(resp.job_id, "duckdb", jobs_svc.STATUS_FAILED)
    assert jobs_svc.rerun(resp.job_id).job_id


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
