"""Tests for the routes service (W7)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ovid2.web.models import PipelineTrigger, PipelineWrite
from ovid2.web.services import pipelines_svc


def _prepare_pipeline_and_address(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake a pipeline + an address config so route validation passes."""
    monkeypatch.setattr(
        "ovid2.web.services.pipelines_svc.pipelines_meta_svc.discover_pipelines",
        lambda: ["ptr"],
    )
    configs_dir = Path(os.environ["OVID2_CONFIGS_DIR"])
    configs_dir.mkdir(parents=True, exist_ok=True)
    (configs_dir / "in.json").write_text(
        '{"src_name": "disc", "src_args": {"path": "/tmp/in"}}',
        encoding="utf-8",
    )
    (configs_dir / "out.json").write_text(
        '{"src_name": "disc", "src_args": {"path": "/tmp/out"}}',
        encoding="utf-8",
    )


def _base_payload(**overrides: object) -> PipelineWrite:
    """Return a valid PipelineWrite with sensible defaults."""
    payload = PipelineWrite(
        pipeline="ptr",
        input_address="in",
        output_address="out",
        trigger=PipelineTrigger(type="manual"),
    )
    return payload.model_copy(update=dict(overrides))


@pytest.mark.unit
def test_write_and_read_manual_route(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    pipelines_svc.write_pipeline("r1", _base_payload(), actor="test", create_only=True)
    detail = pipelines_svc.get_pipeline("r1")
    assert detail is not None
    assert detail.pipeline == "ptr"
    assert detail.trigger.type == "manual"
    # Manual routes never populate next_fire_at.
    assert detail.next_fire_at is None


@pytest.mark.unit
def test_cron_route_populates_next_fire(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    pipelines_svc.write_pipeline(
        "r_cron",
        _base_payload(trigger=PipelineTrigger(type="cron", cron="0 3 * * *")),
        actor="test",
        create_only=True,
    )
    detail = pipelines_svc.get_pipeline("r_cron")
    assert detail is not None
    assert detail.next_fire_at is not None
    when = datetime.fromisoformat(detail.next_fire_at)
    assert when > datetime.now(timezone.utc)


@pytest.mark.unit
def test_interval_next_fire(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    pipelines_svc.write_pipeline(
        "r_int",
        _base_payload(trigger=PipelineTrigger(type="interval", interval_minutes=15)),
        actor="test",
        create_only=True,
    )
    detail = pipelines_svc.get_pipeline("r_int")
    assert detail is not None
    assert detail.next_fire_at is not None
    when = datetime.fromisoformat(detail.next_fire_at)
    delta = when - datetime.now(timezone.utc)
    # Interval-based routes fire ``interval`` from now on first save.
    assert timedelta(minutes=14) <= delta <= timedelta(minutes=16)


@pytest.mark.unit
def test_invalid_cron_rejected(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    with pytest.raises(pipelines_svc.PipelineError, match="cron"):
        pipelines_svc.write_pipeline(
            "r_bad",
            _base_payload(trigger=PipelineTrigger(type="cron", cron="totally not cron")),
            actor="test",
            create_only=True,
        )


@pytest.mark.unit
def test_run_now_submits_job(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    pipelines_svc.write_pipeline("r_manual", _base_payload(), actor="test", create_only=True)
    response = pipelines_svc.run_now("r_manual", actor="test")
    assert response.status == "queued"
    detail = pipelines_svc.get_pipeline("r_manual")
    assert detail is not None
    assert detail.last_job_id == response.job_id


@pytest.mark.unit
def test_run_now_submit_validation_error_becomes_pipeline_error(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A submit-time validation failure surfaces as PipelineError, not 500.

    Regression: a copy pipeline created with empty (open) tables and run
    with no tables override sends ``detail.tables`` verbatim to
    ``jobs_svc.submit``, which rejects the ``['all']`` sentinel with a
    ``SubmitError``. ``run_now`` must re-raise that as a ``PipelineError``
    so the web/API handlers (which only catch ``PipelineError``) return a
    clean 400 instead of letting ``SubmitError`` escape as an uncaught
    Internal Server Error.
    """
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    pipelines_svc.write_pipeline(
        "r_copy_open",
        _base_payload(kind="copy", tables=[]),
        actor="test",
        create_only=True,
    )
    with pytest.raises(pipelines_svc.PipelineError, match="tables"):
        pipelines_svc.run_now("r_copy_open", actor="test")


@pytest.mark.unit
def test_run_now_open_side_requires_override(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An open (empty) side with no override raises rather than submitting."""
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    pipelines_svc.write_pipeline(
        "r_open_in",
        _base_payload(input_address=""),
        actor="test",
        create_only=True,
    )
    with pytest.raises(pipelines_svc.PipelineError, match="input open"):
        pipelines_svc.run_now("r_open_in", actor="test")


@pytest.mark.unit
def test_run_now_override_binds_open_side(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing input_address binds the open side and submits successfully."""
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    pipelines_svc.write_pipeline(
        "r_open_bind",
        _base_payload(input_address=""),
        actor="test",
        create_only=True,
    )
    captured: dict[str, object] = {}

    def _fake_submit(request: object, actor: str = "web") -> object:  # type: ignore[no-untyped-def]
        captured["input_address"] = request.input_address  # type: ignore[attr-defined]
        captured["batch_id"] = request.batch_id  # type: ignore[attr-defined]

        class _Resp:
            job_id = "job-1"
            instance_id = "inst-1"
            status = "queued"

        return _Resp()

    monkeypatch.setattr(pipelines_svc.jobs_svc, "submit", _fake_submit)
    pipelines_svc.run_now("r_open_bind", actor="test", input_address="in", batch_id="sch1")
    assert captured["input_address"] == "in"
    assert captured["batch_id"] == "sch1"


@pytest.mark.unit
def test_run_now_tables_override_makes_open_copy_runnable(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy pipeline created *open* on tables submits once given a list.

    Without the override the run fails validation (see
    ``test_run_now_submit_validation_error_becomes_pipeline_error``); with
    an explicit ``tables`` list it reaches ``jobs_svc.submit`` and the list
    is forwarded verbatim.
    """
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    pipelines_svc.write_pipeline(
        "r_copy_tbl",
        _base_payload(kind="copy", tables=[]),
        actor="test",
        create_only=True,
    )
    captured: dict[str, object] = {}

    def _fake_submit(request: object, actor: str = "web") -> object:  # type: ignore[no-untyped-def]
        captured["tables"] = list(request.tables)  # type: ignore[attr-defined]

        class _Resp:
            job_id = "job-9"
            instance_id = "inst-9"
            status = "queued"

        return _Resp()

    monkeypatch.setattr(pipelines_svc.jobs_svc, "submit", _fake_submit)
    pipelines_svc.run_now("r_copy_tbl", actor="test", tables=["DEMOGRAPHIC", "ENCOUNTER"])
    assert captured["tables"] == ["DEMOGRAPHIC", "ENCOUNTER"]


@pytest.mark.unit
def test_run_now_tables_none_keeps_stored_tables(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``tables=None`` (the default) keeps the config's stored tables."""
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    pipelines_svc.write_pipeline(
        "r_copy_stored",
        _base_payload(kind="copy", tables=["LAB_RESULT_CM"]),
        actor="test",
        create_only=True,
    )
    captured: dict[str, object] = {}

    def _fake_submit(request: object, actor: str = "web") -> object:  # type: ignore[no-untyped-def]
        captured["tables"] = list(request.tables)  # type: ignore[attr-defined]

        class _Resp:
            job_id = "job-10"
            instance_id = "inst-10"
            status = "queued"

        return _Resp()

    monkeypatch.setattr(pipelines_svc.jobs_svc, "submit", _fake_submit)
    pipelines_svc.run_now("r_copy_stored", actor="test")
    assert captured["tables"] == ["LAB_RESULT_CM"]


@pytest.mark.unit
def test_list_pipelines_surfaces_stored_tables(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index summary carries the stored tables so the Run modal can
    prefill them; an open copy pipeline surfaces an empty list (not
    ``['all']``)."""
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    pipelines_svc.write_pipeline(
        "r_copy_open2",
        _base_payload(kind="copy", tables=[]),
        actor="test",
        create_only=True,
    )
    pipelines_svc.write_pipeline(
        "r_etl_default",
        _base_payload(kind="etl", cdm="pcornet_7_0"),
        actor="test",
        create_only=True,
    )
    by_name = {p.name: p for p in pipelines_svc.list_pipelines()}
    assert by_name["r_copy_open2"].tables == []
    # etl-shape kinds keep the ``["all"]`` default.
    assert by_name["r_etl_default"].tables == ["all"]


@pytest.mark.unit
def test_enable_disable(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    pipelines_svc.write_pipeline(
        "r_toggle",
        _base_payload(enabled=True, trigger=PipelineTrigger(type="cron", cron="0 3 * * *")),
        actor="test",
        create_only=True,
    )
    pipelines_svc.set_enabled("r_toggle", False, actor="test")
    detail = pipelines_svc.get_pipeline("r_toggle")
    assert detail is not None
    assert detail.enabled is False
    pipelines_svc.set_enabled("r_toggle", True, actor="test")
    assert (pipelines_svc.get_pipeline("r_toggle") or _base_payload()).enabled is True


@pytest.mark.unit
def test_due_pipelines(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    pipelines_svc.write_pipeline(
        "r_due",
        _base_payload(trigger=PipelineTrigger(type="interval", interval_minutes=1)),
        actor="test",
        create_only=True,
    )
    # Advance the clock 2 minutes into the future.
    future = datetime.now(timezone.utc) + timedelta(minutes=2)
    due = pipelines_svc.due_pipelines(now=future)
    assert any(d.name == "r_due" for d in due)


@pytest.mark.unit
def test_delete_route_clears_state(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    pipelines_svc.write_pipeline(
        "r_del",
        _base_payload(trigger=PipelineTrigger(type="interval", interval_minutes=1)),
        actor="test",
        create_only=True,
    )
    pipelines_svc.delete_pipeline("r_del", actor="test")
    assert pipelines_svc.get_pipeline("r_del") is None
    from ovid2.web import db as web_db

    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM web_pipelines_state WHERE name = ?", ("r_del",)
        ).fetchone()
    assert row is None


@pytest.mark.unit
def test_unknown_pipeline_rejected(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    with pytest.raises(pipelines_svc.PipelineError, match="pipeline"):
        pipelines_svc.write_pipeline(
            "r_bad",
            PipelineWrite(pipeline="not-a-pipeline", input_address="in", output_address="out"),
            actor="test",
            create_only=True,
        )


@pytest.mark.unit
def test_unknown_address_rejected(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    with pytest.raises(pipelines_svc.PipelineError, match="input_address"):
        pipelines_svc.write_pipeline(
            "r_bad",
            PipelineWrite(pipeline="ptr", input_address="nope", output_address="out"),
            actor="test",
            create_only=True,
        )


# ---------------------------------------------------------------------------
# Copy-kind validation (ADR: copy job kind)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_copy_route_scrubs_etl_only_fields(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kind=copy scrubs cdm/transformations/sf_* so the worker argv is clean."""
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    pipelines_svc.write_pipeline(
        "r_copy",
        _base_payload(
            kind="copy",
            cdm="pcornet_7_0",
            tables=["hash_token", "external_meds"],
            transformations=["format", "map"],
            sf_database="X",
            sf_account="Y",
            sf_schema="Z",
        ),
        actor="test",
        create_only=True,
    )
    detail = pipelines_svc.get_pipeline("r_copy")
    assert detail is not None
    assert detail.kind == "copy"
    assert detail.cdm is None, "cdm must be scrubbed for kind=copy"
    assert detail.transformations == [], "transformations must be scrubbed for kind=copy"
    assert detail.sf_database is None
    assert detail.sf_account is None
    assert detail.sf_schema is None
    assert detail.tables == ["hash_token", "external_meds"]


@pytest.mark.unit
def test_copy_route_rejects_all_sentinel(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`tools copy` uses nargs='+' so ['all'] is meaningless — reject at write."""
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    with pytest.raises(pipelines_svc.PipelineError, match="do not support"):
        pipelines_svc.write_pipeline(
            "r_copy_all",
            _base_payload(kind="copy", tables=["all"]),
            actor="test",
            create_only=True,
        )


@pytest.mark.unit
def test_copy_route_accepts_empty_tables_as_open(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty tables on copy is the 'open' choice — tables picked at run time.

    The ``/submit`` form still enforces an explicit list at run time
    (``jobs_svc._validate_submit``); the *config* may defer it.
    """
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    pipelines_svc.write_pipeline(
        "r_copy_empty",
        _base_payload(kind="copy", tables=[]),
        actor="test",
        create_only=True,
    )
    detail = pipelines_svc.get_pipeline("r_copy_empty")
    assert detail is not None
    assert detail.kind == "copy"
    assert detail.tables == []


@pytest.mark.unit
def test_copy_route_accepts_all_for_access_source(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`copy` from an Access source accepts ['all'] — it is expandable.

    Access files are self-describing containers of tables, so the copy
    executor can enumerate them at run time. The web-tier write must not
    reject ``['all']`` for such a source.
    """
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    configs_dir = Path(os.environ["OVID2_CONFIGS_DIR"])
    # A legacy-shape Access address maps ``src_name == 'access'`` →
    # ``AccessDriver.supports_expand_all() == True``.
    (configs_dir / "in_access.json").write_text(
        '{"src_name": "access", "src_args": {"path": "/tmp/v.accdb"}}',
        encoding="utf-8",
    )
    pipelines_svc.write_pipeline(
        "r_copy_all_access",
        _base_payload(kind="copy", tables=["all"], input_address="in_access"),
        actor="test",
        create_only=True,
    )
    detail = pipelines_svc.get_pipeline("r_copy_all_access")
    assert detail is not None
    assert detail.kind == "copy"
    assert detail.tables == ["all"]


@pytest.mark.unit
def test_unknown_kind_rejected(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown kinds (e.g. `passthrough`) still fail with a clear message."""
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    with pytest.raises(pipelines_svc.PipelineError, match="Unsupported pipeline kind"):
        pipelines_svc.write_pipeline(
            "r_bad_kind",
            _base_payload(kind="passthrough", tables=["t"]),
            actor="test",
            create_only=True,
        )


# ---------------------------------------------------------------------------
# Move-kind validation (M1: driver-native successor to `copy`)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_move_route_scrubs_etl_only_fields(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kind=move scrubs cdm/transformations/sf_* — parity with copy."""
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    pipelines_svc.write_pipeline(
        "r_move",
        _base_payload(
            kind="move",
            cdm="pcornet_7_0",
            tables=["hash_token", "external_meds"],
            transformations=["format", "map"],
            sf_database="X",
            sf_account="Y",
            sf_schema="Z",
        ),
        actor="test",
        create_only=True,
    )
    detail = pipelines_svc.get_pipeline("r_move")
    assert detail is not None
    assert detail.kind == "move"
    assert detail.cdm is None
    assert detail.transformations == []
    assert detail.sf_database is None
    assert detail.sf_account is None
    assert detail.sf_schema is None
    assert detail.tables == ["hash_token", "external_meds"]


@pytest.mark.unit
def test_move_route_rejects_all_sentinel(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`tools move` uses nargs='+' so ['all'] is meaningless — parity with copy."""
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    with pytest.raises(pipelines_svc.PipelineError, match="do not support"):
        pipelines_svc.write_pipeline(
            "r_move_all",
            _base_payload(kind="move", tables=["all"]),
            actor="test",
            create_only=True,
        )


@pytest.mark.unit
def test_move_route_accepts_empty_tables_as_open(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty tables on move is the 'open' choice — parity with copy."""
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    pipelines_svc.write_pipeline(
        "r_move_empty",
        _base_payload(kind="move", tables=[]),
        actor="test",
        create_only=True,
    )
    detail = pipelines_svc.get_pipeline("r_move_empty")
    assert detail is not None
    assert detail.kind == "move"
    assert detail.tables == []


# ---------------------------------------------------------------------------
# Package-free kinds (health / copy / move) skip the installed-package gate
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_health_pipeline_without_package_accepted(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A health pipeline is package-free — creation must not require a package.

    Regression for "Pipeline 'EDC_repot' is not installed under
    ovid2_pipelines." raised when creating an EDC/health-report pipeline.
    ``discover_pipelines`` returns only ``["ptr"]`` here, so ``EDC_repot``
    is deliberately *not* installed.
    """
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    pipelines_svc.write_pipeline(
        "EDC_repot",
        _base_payload(pipeline="EDC_repot", kind="health"),
        actor="test",
        create_only=True,
    )
    detail = pipelines_svc.get_pipeline("EDC_repot")
    assert detail is not None
    assert detail.kind == "health"
    assert detail.pipeline == "EDC_repot"


@pytest.mark.unit
def test_copy_move_without_package_accepted(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """copy / move are package-free too — no installed package required."""
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    for kind in ("copy", "move"):
        pipelines_svc.write_pipeline(
            f"nopkg_{kind}",
            _base_payload(
                pipeline=f"nopkg_{kind}",
                kind=kind,
                tables=["hash_token"],
            ),
            actor="test",
            create_only=True,
        )
        detail = pipelines_svc.get_pipeline(f"nopkg_{kind}")
        assert detail is not None
        assert detail.kind == kind


@pytest.mark.unit
def test_health_pipeline_requires_nonempty_pipeline(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even package-free kinds still need a non-empty pipeline name."""
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    with pytest.raises(pipelines_svc.PipelineError, match="pipeline is required"):
        pipelines_svc.write_pipeline(
            "blank",
            _base_payload(pipeline="   ", kind="health"),
            actor="test",
            create_only=True,
        )


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["etl", "analysis"])
def test_package_required_kinds_still_gated(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """etl / analysis still require an installed package."""
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    with pytest.raises(pipelines_svc.PipelineError, match="is not installed under ovid2_pipelines"):
        pipelines_svc.write_pipeline(
            "gated",
            _base_payload(pipeline="not-a-pipeline", kind=kind),
            actor="test",
            create_only=True,
        )


@pytest.mark.unit
def test_dq_checks_is_package_free_and_persists_cycle(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dq_checks needs no installed package; it persists its report cycle.

    The pipeline field is only a label — an id that is *not* an installed
    ``ovid2_pipelines`` package must be accepted — and the chosen cycle
    package round-trips through write → read.
    """
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    pipelines_svc.write_pipeline(
        "edc1",
        _base_payload(pipeline="edc1", kind="dq_checks", packages=["20r1"]),
        actor="test",
        create_only=True,
    )
    detail = pipelines_svc.get_pipeline("edc1")
    assert detail is not None
    assert detail.kind == "dq_checks"
    assert detail.packages == ["20r1"]
    # Health-shaped kinds carry no formatter/mapper/fixer chain.
    assert detail.transformations == []


@pytest.mark.unit
def test_dq_checks_rejects_unknown_cycle(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unregistered report-cycle id is rejected at write time."""
    _prepare_pipeline_and_address(isolated_xdg, monkeypatch)
    with pytest.raises(pipelines_svc.PipelineError, match="report-cycle package"):
        pipelines_svc.write_pipeline(
            "edc_bad",
            _base_payload(pipeline="edc_bad", kind="dq_checks", packages=["99z9"]),
            actor="test",
            create_only=True,
        )
