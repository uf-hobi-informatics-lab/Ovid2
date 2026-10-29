"""Address = exact location: batch-id derivation + the overwrite gate (web tier).

Every address is the exact location its data is read from / written to.
The batch id is the complete Data Space path of the input location, and a
run into an output that already holds tables needs an explicit
confirmation (409 → ``confirm_overwrite``; agents → ``run.overwrite``
human approval).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from ovid2.web.models import JobSubmitRequest
from ovid2.web.services import jobs_svc
from tests.helpers.output_probe import set_output_tables


@pytest.fixture()
def setup(isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A stub pipeline package + a disc storage account with two exact-location addresses."""
    root = tmp_path / "pipelines_root"
    pkg = root / "ovid2_pipelines" / "tp_exact"
    (pkg / "formatters").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "pipeline_settings.py").write_text("mappers_type = 'pcornet_mappers'\n")
    monkeypatch.syspath_prepend(str(root))
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]

    data = tmp_path / "data"
    (data / "landing" / "2026_03").mkdir(parents=True)
    conns = Path(os.environ["OVID2_CONNECTIONS_DIR"])
    (conns / "inbox.json").write_text(
        json.dumps({"type": "disc", "args": {"host_path": str(data)}}), encoding="utf-8"
    )
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    (configs / "in_exact.json").write_text(
        json.dumps({"connection": "inbox", "location": {"path": "landing/2026_03"}})
    )
    (configs / "out_exact.json").write_text(
        json.dumps({"connection": "inbox", "location": {"path": "out/omop"}})
    )
    (configs / "legacy_in.json").write_text(
        json.dumps({"src_name": "disc", "src_args": {"path": "/tmp/legacy"}})
    )
    yield "tp_exact"
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]


def _req(pipeline: str, **kw: object) -> JobSubmitRequest:
    base: dict[str, object] = {
        "pipeline": pipeline,
        "batch_id": "typed-by-user",
        "input_address": "in_exact",
        "output_address": "out_exact",
    }
    base.update(kw)
    return JobSubmitRequest(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------- batch id


@pytest.mark.unit
def test_batch_id_is_the_input_data_space_path(setup: str) -> None:
    resp = jobs_svc.submit(_req(setup))
    detail = jobs_svc.get_job(resp.job_id)
    assert detail is not None
    # Derived from the input address — the typed value is ignored.
    assert detail.batch_id == "inbox/landing/2026_03"


@pytest.mark.unit
def test_batch_id_from_runtime_connection_location(setup: str) -> None:
    resp = jobs_svc.submit(
        _req(setup, input_address="", input_connection="inbox", input_location="landing/2026_03")
    )
    detail = jobs_svc.get_job(resp.job_id)
    assert detail is not None and detail.batch_id == "inbox/landing/2026_03"


@pytest.mark.unit
def test_legacy_input_keeps_the_supplied_label(setup: str) -> None:
    resp = jobs_svc.submit(_req(setup, input_address="legacy_in", batch_id="20260101"))
    detail = jobs_svc.get_job(resp.job_id)
    assert detail is not None and detail.batch_id == "20260101"


@pytest.mark.unit
def test_legacy_input_without_label_is_rejected(setup: str) -> None:
    with pytest.raises(jobs_svc.SubmitError, match="could not be derived"):
        jobs_svc.submit(_req(setup, input_address="legacy_in", batch_id=""))


# ---------------------------------------------------------- sf_* overrides


@pytest.mark.unit
def test_sf_schema_folds_into_runtime_location(setup: str) -> None:
    req = _req(
        setup,
        input_address="",
        input_connection="inbox",
        input_location="DB",
        sf_schema="SITE_X",
    )
    jobs_svc._fold_legacy_sf_location(req)
    assert req.input_location == "DB/SITE_X"
    assert req.sf_schema is None


@pytest.mark.unit
def test_sf_schema_without_runtime_location_is_rejected(setup: str) -> None:
    with pytest.raises(jobs_svc.SubmitError, match="no longer overrides"):
        jobs_svc.submit(_req(setup, sf_schema="SITE_X"))


# --------------------------------------------------------- overwrite gate


@pytest.mark.unit
def test_occupied_output_requires_confirmation(setup: str, monkeypatch: pytest.MonkeyPatch) -> None:
    set_output_tables(monkeypatch, ["DEMOGRAPHIC", "ENCOUNTER"])
    with pytest.raises(jobs_svc.OverwriteRequired) as excinfo:
        jobs_svc.submit(_req(setup))
    assert excinfo.value.tables == ["DEMOGRAPHIC", "ENCOUNTER"]
    assert excinfo.value.output_address == "out_exact"


@pytest.mark.unit
def test_confirmed_overwrite_is_persisted_for_the_executor(
    setup: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_output_tables(monkeypatch, ["DEMOGRAPHIC"])
    resp = jobs_svc.submit(_req(setup, confirm_overwrite=True))
    detail = jobs_svc.get_job(resp.job_id)
    assert detail is not None and detail.extra_args.get("overwrite") is True
    from ovid2.web.worker import Worker

    assert Worker(ovid2_bin=["ovid2"])._build_argv(detail)[-1] == "--overwrite"


@pytest.mark.unit
def test_empty_or_unknown_output_does_not_block(
    setup: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_output_tables(monkeypatch, [])
    detail = jobs_svc.get_job(jobs_svc.submit(_req(setup)).job_id)
    assert detail is not None and "overwrite" not in detail.extra_args
    set_output_tables(monkeypatch, None)  # cannot be determined
    assert jobs_svc.submit(_req(setup)).status == "queued"


@pytest.mark.unit
def test_copy_only_counts_tables_it_writes(setup: str, monkeypatch: pytest.MonkeyPatch) -> None:
    set_output_tables(monkeypatch, ["OTHER_TABLE"])
    resp = jobs_svc.submit(_req(setup, kind="copy", tables=["demographic"], transformations=[]))
    assert resp.status == "queued"
    set_output_tables(monkeypatch, ["DEMOGRAPHIC"])
    with pytest.raises(jobs_svc.OverwriteRequired):
        jobs_svc.submit(_req(setup, kind="copy", tables=["demographic"], transformations=[]))


@pytest.mark.unit
def test_rerun_hits_the_gate_again(setup: str, monkeypatch: pytest.MonkeyPatch) -> None:
    set_output_tables(monkeypatch, [])
    first = jobs_svc.submit(_req(setup))
    jobs_svc.mark_terminal(first.job_id, status=jobs_svc.STATUS_DONE)
    set_output_tables(monkeypatch, ["DEMOGRAPHIC"])  # the first run's output
    with pytest.raises(jobs_svc.OverwriteRequired):
        jobs_svc.rerun(first.job_id)
    second = jobs_svc.rerun(first.job_id, confirm_overwrite=True)
    detail = jobs_svc.get_job(second.job_id)
    assert detail is not None and detail.extra_args.get("overwrite") is True


@pytest.mark.unit
def test_draft_is_checked_at_launch(setup: str, monkeypatch: pytest.MonkeyPatch) -> None:
    set_output_tables(monkeypatch, ["DEMOGRAPHIC"])
    draft = jobs_svc.create_draft(_req(setup))  # drafts are not gated when stored
    with pytest.raises(jobs_svc.OverwriteRequired):
        jobs_svc.launch_draft(draft.job_id)
    detail = jobs_svc.launch_draft(draft.job_id, confirm_overwrite=True)
    assert detail.status == "queued" and detail.extra_args.get("overwrite") is True


# ------------------------------------------------------------- HTTP / API


@pytest.mark.unit
def test_api_409_then_confirm(setup: str, web_client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    set_output_tables(monkeypatch, ["DEMOGRAPHIC"])
    payload = {"pipeline": setup, "input_address": "in_exact", "output_address": "out_exact"}
    r = web_client.post("/api/v1/runs", json=payload)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["status"] == "overwrite_required"
    assert detail["existing_tables"] == ["DEMOGRAPHIC"]
    r = web_client.post("/api/v1/runs", json={**payload, "confirm_overwrite": True})
    assert r.status_code == 201, r.text
    run = web_client.get(f"/api/v1/jobs/{r.json()['job_id']}").json()
    assert run["batch_id"] == "inbox/landing/2026_03"


@pytest.mark.unit
def test_runs_lookup_accepts_slashed_batch_id(setup: str, web_client) -> None:  # type: ignore[no-untyped-def]
    payload = {"pipeline": setup, "input_address": "in_exact", "output_address": "out_exact"}
    instance = web_client.post("/api/v1/runs", json=payload).json()["instance_id"]
    r = web_client.get("/api/v1/runs/inbox/landing/2026_03")
    assert r.status_code == 200, r.text
    assert r.json()["instance_id"] == instance
    # Sub-routes are not shadowed by the slash-tolerant lookup.
    assert web_client.get(f"/api/v1/runs/{instance}/timeline").status_code != 404


@pytest.mark.unit
def test_agent_confirm_needs_human_approval(setup: str, web_client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """An agent principal's confirm_overwrite becomes a run.overwrite approval (202)."""
    from ovid2.web.auth import deps
    from ovid2.web.auth.roles import Role

    monkeypatch.setattr(
        deps,
        "current_user",
        lambda _req: deps.AuthContext(user_id="mcp:service", role=Role.SYSTEM_ADMIN),
    )
    set_output_tables(monkeypatch, ["DEMOGRAPHIC"])
    payload = {
        "pipeline": setup,
        "input_address": "in_exact",
        "output_address": "out_exact",
        "confirm_overwrite": True,
    }
    r = web_client.post("/api/v1/runs", json=payload)
    assert r.status_code == 202, r.text
    approval = r.json()["detail"]
    assert approval["action"] == "run.overwrite"
    from ovid2.web.models import ApprovalDecision
    from ovid2.web.services import approvals_svc

    approvals_svc.decide(approval["approval_id"], True, ApprovalDecision(decided_by="human"))
    r = web_client.post("/api/v1/runs", json=payload)
    assert r.status_code == 201, r.text


@pytest.mark.unit
def test_submit_form_renders_confirmation(setup: str, web_client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    set_output_tables(monkeypatch, ["DEMOGRAPHIC"])
    form = {
        "pipeline": setup,
        "kind": "etl",
        "input_mode": "address",
        "input_address": "in_exact",
        "output_mode": "address",
        "output_address": "out_exact",
        "tables": "all",
        "transformations": "all",
    }
    r = web_client.post("/submit", data=form, follow_redirects=False)
    assert r.status_code == 409
    assert "Overwrite existing data?" in r.text
    assert 'name="confirm_overwrite" value="true"' in r.text
    assert 'name="output_address" value="out_exact"' in r.text  # fields re-posted
    r = web_client.post(
        "/submit", data={**form, "confirm_overwrite": "true"}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/runs/")


@pytest.mark.unit
def test_submit_page_shows_derived_batch_for_saved_address(setup: str, web_client) -> None:  # type: ignore[no-untyped-def]
    body = web_client.get("/submit").text
    assert 'data-batch="inbox/landing/2026_03"' in body
    assert 'id="batch-field" readonly' in body
    assert 'name="sf_schema"' not in body and 'name="sf_database"' not in body


# ------------------------------------------------- run-page popup (fetch)


@pytest.mark.unit
def test_launch_fetch_409_then_confirm(setup: str, web_client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The run-page ▶ (fetch) gets JSON 409 for the popup; confirm queues the draft."""
    set_output_tables(monkeypatch, ["DEMOGRAPHIC", "ENCOUNTER"])
    draft = jobs_svc.create_draft(_req(setup))
    url = f"/jobs/{draft.job_id}/launch"
    r = web_client.post(url, headers={"X-Requested-With": "fetch"}, follow_redirects=False)
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["status"] == "overwrite_required"
    assert body["existing_tables"] == ["DEMOGRAPHIC", "ENCOUNTER"]
    assert body["output_address"] == "out_exact"
    assert body["confirm_url"] == url
    assert jobs_svc.get_job(draft.job_id).status == "draft"  # type: ignore[union-attr]
    # The popup's "Overwrite and run" re-POSTs with confirm_overwrite=true.
    r = web_client.post(
        url,
        data={"confirm_overwrite": "true"},
        headers={"X-Requested-With": "fetch"},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"
    detail = jobs_svc.get_job(draft.job_id)
    assert detail is not None and detail.extra_args.get("overwrite") is True


@pytest.mark.unit
def test_rerun_fetch_409_then_confirm(setup: str, web_client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    set_output_tables(monkeypatch, [])
    first = jobs_svc.submit(_req(setup))
    jobs_svc.mark_terminal(first.job_id, status=jobs_svc.STATUS_DONE)
    set_output_tables(monkeypatch, ["DEMOGRAPHIC"])
    url = f"/jobs/{first.job_id}/rerun"
    r = web_client.post(url, headers={"X-Requested-With": "fetch"}, follow_redirects=False)
    assert r.status_code == 409 and r.json()["status"] == "overwrite_required"
    assert r.json()["confirm_url"] == url
    r = web_client.post(
        url,
        data={"confirm_overwrite": "true"},
        headers={"X-Requested-With": "fetch"},
        follow_redirects=False,
    )
    assert r.status_code == 201, r.text
    detail = jobs_svc.get_job(r.json()["job_id"])
    assert detail is not None and detail.extra_args.get("overwrite") is True


@pytest.mark.unit
def test_run_page_carries_overwrite_popup(setup: str, web_client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Draft and finished run pages both ship the (hidden) overwrite popup."""
    set_output_tables(monkeypatch, [])
    draft = jobs_svc.create_draft(_req(setup))
    page = web_client.get(f"/runs/{draft.instance_id}").text
    assert "data-overwrite-modal" in page and "data-overwrite-confirm" in page
    done = jobs_svc.submit(_req(setup))
    jobs_svc.mark_terminal(done.job_id, status=jobs_svc.STATUS_DONE)
    page = web_client.get(f"/runs/{done.instance_id}").text
    assert "data-overwrite-modal" in page and 'data-action="rerun"' in page
