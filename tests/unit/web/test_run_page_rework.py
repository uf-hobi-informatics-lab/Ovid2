"""HTTP tests for the reworked run-detail page.

Covers the changes to ``/runs/{id}``:

* The redundant "Submitted job <id>" flash bar is gone.
* The job-details box surfaces Engine, Input and Output addresses.
* The elapsed-clock anchor (``data-started-at-ms``) is server-rendered.
* The action menu bar shows Cancel while queued/running and Rerun once
  terminal.
* The split lower region (per-table summary + table detail) is present.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def pipeline_env_run(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    """Register a stub pipeline + two disc address configs."""
    root = tmp_path / "pipelines_root"
    pkg = root / "ovid2_pipelines" / "tp_run"
    (pkg / "__init__.py").parent.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "pipeline_settings.py").write_text(
        "mappers_type='pcornet_mappers'\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(root))
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    (configs / "in_run.json").write_text(
        json.dumps({"src_name": "disc", "src_args": {"path": "/tmp/i"}}),
        encoding="utf-8",
    )
    (configs / "out_run.json").write_text(
        json.dumps({"src_name": "disc", "src_args": {"path": "/tmp/o"}}),
        encoding="utf-8",
    )
    yield "tp_run"
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]


def _submit(web_client: object, pipeline: str, *, engine: str = "snowpark") -> dict:
    payload = {
        "pipeline": pipeline,
        "batch_id": "B1",
        "input_address": "in_run",
        "output_address": "out_run",
        "engine": engine,
    }
    resp = web_client.post("/api/v1/runs", json=payload)  # type: ignore[attr-defined]
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.unit
def test_run_page_shows_engine_input_output(
    web_client: object, pipeline_env_run: str
) -> None:
    job = _submit(web_client, pipeline_env_run, engine="snowpark")
    resp = web_client.get(f"/runs/{job['instance_id']}")  # type: ignore[attr-defined]
    assert resp.status_code == 200
    html = resp.text
    assert ">Engine<" in html
    assert "snowpark" in html
    assert ">Input<" in html
    assert "in_run" in html
    assert ">Output<" in html
    assert "out_run" in html


@pytest.mark.unit
def test_run_page_has_no_submitted_flash(
    web_client: object, pipeline_env_run: str
) -> None:
    """The 'Submitted job <id>' flash bar is removed."""
    job = _submit(web_client, pipeline_env_run)
    # Even if a stale ?flash= is passed, the page must not render it.
    resp = web_client.get(  # type: ignore[attr-defined]
        f"/runs/{job['instance_id']}?flash=Submitted+job+{job['job_id']}"
    )
    html = resp.text
    assert "flash-ok" not in html
    assert "Submitted job" not in html


@pytest.mark.unit
def test_submit_redirect_drops_flash(
    web_client: object, pipeline_env_run: str
) -> None:
    resp = web_client.post(  # type: ignore[attr-defined]
        "/submit",
        data={
            "pipeline": pipeline_env_run,
            "batch_id": "B2",
            "input_address": "in_run",
            "output_address": "out_run",
            "engine": "pyspark",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "?flash=" not in resp.headers["location"]


@pytest.mark.unit
def test_run_page_menu_shows_cancel_when_queued(
    web_client: object, pipeline_env_run: str
) -> None:
    job = _submit(web_client, pipeline_env_run)
    resp = web_client.get(f"/runs/{job['instance_id']}")  # type: ignore[attr-defined]
    html = resp.text
    # The prompt cursor IS the transport control now: no separate
    # top-right toolbar. While queued/running it posts /cancel and shows
    # the Lucide "stop" (■) glyph.
    assert "term-menu" not in html
    assert "term-prompt-btn" in html
    assert f"/jobs/{job['job_id']}/cancel" in html
    assert 'aria-label="Cancel"' in html
    assert "icon-stop" in html
    # The transport control lives in the top summary block, ahead of the
    # split lower region.
    assert "term-summary" in html
    assert "term-split" in html
    summary_part = html.split("term-split", 1)[0]
    assert (
        "term-prompt-btn" in summary_part
    ), "prompt transport control must be in the top summary block"
    # The instance id sits at the right edge, after the flex spacer.
    assert "term-flex" in summary_part
    flex_part = summary_part.split("term-flex", 1)[1]
    assert "term-summary-run" in flex_part, "instance id must render on the right"


@pytest.mark.unit
def test_run_page_menu_shows_rerun_when_terminal(
    web_client: object, pipeline_env_run: str
) -> None:
    from ovid2.web.services import jobs_svc

    job = _submit(web_client, pipeline_env_run)
    jobs_svc.mark_terminal(job["job_id"], status="failed", error="x")
    resp = web_client.get(f"/runs/{job['instance_id']}")  # type: ignore[attr-defined]
    html = resp.text
    assert "term-menu" not in html
    assert "term-prompt-btn" in html
    assert f"/jobs/{job['job_id']}/rerun" in html
    assert 'aria-label="Rerun"' in html
    assert "icon-play" in html


@pytest.mark.unit
def test_run_page_shows_rerun_when_cancelled(
    web_client: object, pipeline_env_run: str
) -> None:
    """A cancelled job renders the ▶ Play (rerun) glyph, not ■ Stop.

    Regression: a cancelled run whose worker was killed before it could
    emit ``instance.finished`` leaves an event log ending on a stale
    ``instance.started``. The pane must carry ``data-status="cancelled"``
    (terminal) and render the Play glyph so the client seeds the correct
    transport control up front.
    """
    from ovid2.web.services import jobs_svc

    job = _submit(web_client, pipeline_env_run)
    jobs_svc.mark_terminal(job["job_id"], status="cancelled")
    resp = web_client.get(f"/runs/{job['instance_id']}")  # type: ignore[attr-defined]
    html = resp.text
    assert 'data-status="cancelled"' in html
    assert f"/jobs/{job['job_id']}/rerun" in html
    assert 'aria-label="Rerun"' in html
    assert "icon-play" in html
    assert "icon-stop" not in html


@pytest.mark.unit
def test_mount_js_seeds_glyph_from_terminal_status() -> None:
    """mount.js must not let replayed history flip a terminal run's glyph.

    Regression: a cancelled run's event log still contains the launch-time
    ``instance.started``. Without the terminal-on-load guard the reader
    replays it, drives the ovid2 label to "running", and flips the
    transport glyph back to a ■ Stop square — and because the events
    endpoint never reports ``finished`` (no ``instance.finished`` event),
    nothing corrects it. The fix seeds the glyph from the server status
    and short-circuits ``applyStatusFromEvent`` when terminal-on-load.
    """
    p = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "ovid2"
        / "web"
        / "static"
        / "display"
        / "terminal"
        / "mount.js"
    )
    text = p.read_text(encoding="utf-8")
    # The glyph is seeded from the server-provided terminal status.
    assert "if (startedTerminal) {\n    flipPrompt(initialStatus);" in text
    # Replayed history never re-drives a terminal-on-load run.
    guard = text.split("function applyStatusFromEvent", 1)[1]
    guard_body = guard.split("function ", 1)[0]
    assert "if (startedTerminal) return;" in guard_body



@pytest.mark.unit
def test_run_page_has_started_at_anchor(
    web_client: object, pipeline_env_run: str
) -> None:
    """The elapsed clock anchor is server-rendered once the job starts."""
    from ovid2.web.services import jobs_svc

    job = _submit(web_client, pipeline_env_run)
    # Move to running so started_at is populated.
    jobs_svc.claim_next_queued()
    resp = web_client.get(f"/runs/{job['instance_id']}")  # type: ignore[attr-defined]
    html = resp.text
    assert "data-started-at-ms=" in html
    assert "data-server-now-ms=" in html


@pytest.mark.unit
def test_run_page_has_split_region(
    web_client: object, pipeline_env_run: str
) -> None:
    job = _submit(web_client, pipeline_env_run)
    resp = web_client.get(f"/runs/{job['instance_id']}")  # type: ignore[attr-defined]
    html = resp.text
    assert "term-split" in html
    assert 'id="table-summary"' in html
    assert 'id="table-detail"' in html
    # The summary list carries a bottom-of-list toggle for auto-follow.
    assert 'id="table-follow-toggle"' in html


@pytest.mark.unit
@pytest.mark.parametrize(
    ("run_kind", "expect_check", "expect_table"),
    [
        ("health", True, False),
        ("dq_checks", True, False),
        ("etl", False, True),
        ("analysis", False, True),
    ],
)
def test_terminal_pane_panel_matches_run_kind(
    run_kind: str, expect_check: bool, expect_table: bool
) -> None:
    """``health`` and ``dq_checks`` mount the check panel; ETL-shaped
    kinds mount the per-table panel. Every kind shows the same shared
    "waiting for data" placeholder so no console feels different.

    ``dq_checks`` runs the identical ``HealthCheckRunner`` and emits
    ``check.*`` events, so it must render ``#check-summary`` /
    ``#check-detail`` (mounted by ``mountCheckPanel``) — not the ETL
    per-table DOM, whose panel filters out check events.
    """
    from ovid2.web.templating import templates

    tpl = templates.env.get_template("_terminal_pane.html")
    html = tpl.render(
        job=None,
        instance_id="20260911-000000-abcdef",
        report_available=False,
        run_kind=run_kind,
    )
    assert ('id="check-summary"' in html) is expect_check
    assert ('id="check-detail"' in html) is expect_check
    assert ('id="table-summary"' in html) is expect_table
    assert ('id="table-detail"' in html) is expect_table
    # Every panel type — current and future — shows the *same* shared
    # "waiting for data" component (summary + detail = 6 pulsing dots),
    # so no pipeline type's console feels different. The old per-kind
    # "waiting for checks…" text is gone.
    assert "waiting for checks" not in html
    assert "data-waiting-logo" in html
    assert "waiting for data" in html
    assert html.count("data-waiting-dot") == 6


@pytest.mark.unit
def test_live_js_imports_table_panel() -> None:
    p = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "ovid2"
        / "web"
        / "static"
        / "live.js"
    )
    text = p.read_text(encoding="utf-8")
    assert "display/tables/panel.js" in text


@pytest.mark.unit
def test_table_panel_module_served(web_client: object) -> None:
    resp = web_client.get("/static/display/tables/panel.js")  # type: ignore[attr-defined]
    assert resp.status_code == 200
    assert "mountTablePanel" in resp.text


@pytest.mark.unit
def test_run_page_seeds_planned_tables_attr(
    web_client: object, pipeline_env_run: str
) -> None:
    """The pane carries ``data-planned-tables`` so the roster pre-seeds.

    A default submit uses the ``["all"]`` sentinel; the attribute is
    still emitted (the panel treats ``["all"]`` as "fetch the plan").
    """
    job = _submit(web_client, pipeline_env_run)
    resp = web_client.get(f"/runs/{job['instance_id']}")  # type: ignore[attr-defined]
    html = resp.text
    assert "data-planned-tables=" in html


@pytest.mark.unit
def test_run_page_pipeline_attr_falls_back_to_job(
    web_client: object, pipeline_env_run: str
) -> None:
    """``data-pipeline`` must be populated from the job when no runs row.

    A freshly-queued job has no runs-table row yet, so the pane's
    ``data-pipeline`` used to render empty — starving the table-roster
    plan fetch (which reads ``data-pipeline``) so nothing pre-seeded.
    It must fall back to ``job.pipeline``.
    """
    job = _submit(web_client, pipeline_env_run)
    resp = web_client.get(f"/runs/{job['instance_id']}")  # type: ignore[attr-defined]
    html = resp.text
    assert f'data-pipeline="{pipeline_env_run}"' in html


@pytest.mark.unit
def test_table_panel_renders_status_dot_not_word() -> None:
    """The summary row uses a colour-coded dot, seeded via the plan API."""
    p = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "ovid2"
        / "web"
        / "static"
        / "display"
        / "tables"
        / "panel.js"
    )
    text = p.read_text(encoding="utf-8")
    # Dot replaces the old ``table-row-status badge`` word.
    assert "table-row-dot" in text
    assert "table-row-status badge" not in text
    # Roster pre-seeding wires the plan endpoint + planned-tables attr.
    assert "/pipeline-packages/" in text
    assert "plannedTables" in text


def _static(*parts: str) -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "src"
        / "ovid2"
        / "web"
        / "static"
        / Path(*parts)
    )


@pytest.mark.unit
def test_shared_run_panel_module_served(web_client: object) -> None:
    """The unified panel behind every pipeline type's console is served."""
    resp = web_client.get("/static/display/run/panel.js")  # type: ignore[attr-defined]
    assert resp.status_code == 200
    assert "mountRunPanel" in resp.text


@pytest.mark.unit
def test_panels_are_thin_wrappers_over_shared_panel() -> None:
    """Both consoles delegate to the shared panel (no copy-pasted behaviour).

    The universal features — status dots, follow toggle, timer, roster —
    live in ``display/run/panel.js`` and both wrappers import
    ``mountRunPanel`` from it rather than re-implementing the loop.
    """
    tables = _static("display", "tables", "panel.js").read_text(encoding="utf-8")
    checks = _static("display", "checks", "panel.js").read_text(encoding="utf-8")
    for text in (tables, checks):
        assert "display/run/panel.js" in text
        assert "mountRunPanel(" in text


@pytest.mark.unit
def test_check_panel_uses_dots_and_follow_toggle() -> None:
    """The checks console matches the ETL console: status dots (not the
    old pill badge), the universal follow-run toggle button, a flat
    roster (no CDC-part group header), a pre-seed roster, and the live
    timer."""
    text = _static("display", "checks", "panel.js").read_text(encoding="utf-8")
    # Colour-coded dot, same vocabulary as the table console.
    assert "check-row-dot" in text
    # The follow toggle is wired for checks too (universal).
    assert "check-follow-toggle" in text
    # Flat roster now — the old CDC-part grouping / header is gone.
    assert "Part 1 · Structural" not in text
    assert "groupLabel" not in text
    # The roster pre-seeds from the run's check selection.
    assert "roster:" in text
    assert "resolveCheckRoster" in text
    # The live MM:SS.s timer is enabled (checks now carry elapsed_ms).
    assert "timed: true" in text


@pytest.mark.unit
def test_check_panel_dot_and_exception_color_in_css() -> None:
    """The shared dot colour set covers both consoles incl. the amber
    ``exception`` state unique to health checks."""
    css = _static("ovid2.css").read_text(encoding="utf-8")
    assert ".check-row-dot" in css
    assert ".table-row-dot.status-exception" in css
    assert ".check-row-dot.status-exception" in css


@pytest.mark.unit
@pytest.mark.parametrize("run_kind", ["health", "dq_checks"])
def test_health_branch_has_follow_toggle(run_kind: str) -> None:
    """The follow-run button is now universal — the health/dq_checks
    branch renders ``#check-follow-toggle`` (previously ETL-only), and the
    divergent ``term-split-health`` class is gone so both branches share
    the identical ``.term-split`` geometry."""
    from ovid2.web.templating import templates

    tpl = templates.env.get_template("_terminal_pane.html")
    html = tpl.render(
        job=None,
        instance_id="20260911-000000-abcdef",
        report_available=False,
        run_kind=run_kind,
    )
    assert 'id="check-follow-toggle"' in html
    assert "term-split-health" not in html


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ctx_key", "attr"),
    [
        ("check_parts", "data-check-parts"),
        ("check_packages", "data-check-packages"),
        ("check_checks", "data-check-checks"),
    ],
)
def test_health_pane_seeds_check_selection_attrs(ctx_key: str, attr: str) -> None:
    """The checks console pane carries the run's check selection so the
    roster pre-seeds (the checks-console analogue of
    ``data-planned-tables``). Absent selections emit no attribute."""
    from ovid2.web.templating import templates

    tpl = templates.env.get_template("_terminal_pane.html")
    # Absent selection ⇒ no attribute.
    html_absent = tpl.render(
        job=None,
        instance_id="20260911-000000-abcdef",
        report_available=False,
        run_kind="health",
    )
    assert attr not in html_absent
    # Present selection ⇒ JSON-encoded attribute.
    html = tpl.render(
        job=None,
        instance_id="20260911-000000-abcdef",
        report_available=False,
        run_kind="health",
        **{ctx_key: ["part1", "integrity"]},
    )
    assert f"{attr}=" in html
    assert "part1" in html


# ---------------------------------------------------------------------------
# Merged runs workspace (list + console)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_runs_workspace_has_list_and_console(
    web_client: object, pipeline_env_run: str
) -> None:
    """GET /runs renders the master-detail workspace: a run-history list
    on the left and the live console on the right (same page)."""
    job = _submit(web_client, pipeline_env_run)
    resp = web_client.get("/runs")  # type: ignore[attr-defined]
    assert resp.status_code == 200
    html = resp.text
    # Left list.
    assert 'class="run-list"' in html
    assert 'class="run-list-items"' in html
    assert 'data-run-nav' in html
    # Batch id (primary sub-bar) + pipeline name (primary label).
    assert "B1" in html
    assert pipeline_env_run in html
    # Right console region + the console itself (auto-selected newest run).
    assert 'id="run-detail-region"' in html
    assert 'id="run-console"' in html
    assert "term-split" in html
    # The auto-selected row is marked.
    assert "is-selected" in html
    assert job["instance_id"]


@pytest.mark.unit
def test_run_detail_fetch_mode_returns_console_fragment_only(
    web_client: object, pipeline_env_run: str
) -> None:
    """A ``X-Requested-With: fetch`` GET of /runs/{id} returns ONLY the
    console fragment (no page shell / no list) so live.js can swap the
    detail region in place."""
    job = _submit(web_client, pipeline_env_run)
    resp = web_client.get(  # type: ignore[attr-defined]
        f"/runs/{job['instance_id']}",
        headers={"X-Requested-With": "fetch"},
    )
    assert resp.status_code == 200
    html = resp.text
    # Console fragment present.
    assert 'id="run-console"' in html
    assert "term-split" in html
    # But NOT the page shell or the list.
    assert "<!DOCTYPE html>" not in html
    assert 'class="run-list"' not in html


@pytest.mark.unit
def test_run_detail_full_page_is_workspace(
    web_client: object, pipeline_env_run: str
) -> None:
    """A plain (non-fetch) GET of /runs/{id} renders the full workspace
    with that run pre-selected."""
    job = _submit(web_client, pipeline_env_run)
    resp = web_client.get(f"/runs/{job['instance_id']}")  # type: ignore[attr-defined]
    assert resp.status_code == 200
    html = resp.text
    assert "<!DOCTYPE html>" in html
    assert 'class="run-list"' in html
    assert 'id="run-console"' in html


@pytest.mark.unit
def test_runs_list_partial_returns_rows_only(
    web_client: object, pipeline_env_run: str
) -> None:
    """The infinite-scroll partial emits only <li> rows, no page shell."""
    _submit(web_client, pipeline_env_run)
    resp = web_client.get("/partials/runs/list?offset=0")  # type: ignore[attr-defined]
    assert resp.status_code == 200
    html = resp.text
    assert "<!DOCTYPE html>" not in html
    assert 'class="run-list"' not in html
    # Rows carry the nav hook.
    assert "run-list-item" in html


@pytest.mark.unit
def test_jobs_url_redirects_to_runs(
    web_client: object, pipeline_env_run: str
) -> None:
    """Legacy /jobs 302-redirects into the merged workspace."""
    resp = web_client.get(  # type: ignore[attr-defined]
        "/jobs", follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/runs"

