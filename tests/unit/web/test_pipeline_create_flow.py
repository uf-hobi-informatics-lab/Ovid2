"""New-pipeline create flow — simplified form + scaffold-then-create.

Exercises ``GET /pipelines/new`` (the redesigned create form) and
``POST /pipelines`` (which scaffolds an ETL package named after the
pipeline, resolves the browsed storage accounts into addresses, and
writes the config binding ``pipeline == name``). Also covers the
``watch`` trigger now exposed on the edit form and the specific-batch
manual run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def temp_pipelines_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the scaffolder at a throwaway pipelines directory.

    Also add that directory to the ``ovid2_pipelines`` namespace
    ``__path__`` so ``pipelines_meta_svc.discover_pipelines()`` (which
    ``pipelines_svc._validate_write`` consults) sees freshly-scaffolded
    packages — mirroring production, where the scaffolder writes into a
    directory already on the namespace path.
    """
    import importlib

    root = tmp_path / "pipelines_out"
    root.mkdir()
    monkeypatch.setattr("ovid2.tools.new_pipeline._pipelines_root", lambda: root)
    pkg = importlib.import_module("ovid2_pipelines")
    original = list(pkg.__path__)
    pkg.__path__.append(str(root))  # type: ignore[attr-defined]
    yield root
    pkg.__path__[:] = original  # type: ignore[attr-defined]



def _write_disc_connection(xdg: Path, name: str, host_path: str) -> None:
    conndir = xdg / "config" / "ovid2" / "connections"
    conndir.mkdir(parents=True, exist_ok=True)
    (conndir / f"{name}.json").write_text(
        json.dumps(
            {
                "type": "disc",
                "args": {
                    "host_path": host_path,
                    "container_path": "/data",
                    "mount_mode": "rw",
                },
            }
        )
    )


@pytest.mark.unit
def test_new_form_has_no_package_dropdown(web_client) -> None:  # type: ignore[no-untyped-def]
    """The create form asks for a type/template, not a list of packages."""
    r = web_client.get("/pipelines/new")
    assert r.status_code == 200
    body = r.text
    assert "New pipeline" in body
    assert "Pipeline type" in body
    # ETL template picker is present…
    assert 'name="template"' in body
    assert "PCORnet ETL" in body
    # …but the old "select an installed package" dropdown is gone.
    assert '<select id="pipeline"' not in body
    assert 'name="pipeline"' not in body
    # watch is the default trigger.
    assert 'value="watch" selected' in body
    # storage picker + browse.js reused.
    assert 'data-storage-picker="input"' in body
    assert 'data-storage-picker="output"' in body
    assert "/static/browse.js" in body


@pytest.mark.unit
def test_create_etl_pipeline_scaffolds_package_named_after_pipeline(
    web_client, isolated_xdg: Path, tmp_path: Path, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "disc_data"
    (root / "2026_01").mkdir(parents=True)
    _write_disc_connection(isolated_xdg, "local_disc", str(root))

    r = web_client.post(
        "/pipelines",
        data={
            "name": "myhosp",
            "kind": "etl",
            "template": "pcornet_etl",
            "input_connection": "local_disc",
            "input_location": "",
            "output_connection": "local_disc",
            "output_location": "out",
            "engine": "pyspark",
            "trigger_type": "watch",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/pipelines/myhosp"

    # A package was scaffolded under the pipeline name.
    assert (temp_pipelines_root / "myhosp" / "pipeline_settings.py").is_file()

    # The config was written binding pipeline == name, watch trigger.
    cfg_path = (
        isolated_xdg / "config" / "ovid2" / "pipelines" / "myhosp.json"
    )
    assert cfg_path.is_file()
    doc = json.loads(cfg_path.read_text())
    assert doc["pipeline"] == "myhosp"
    assert doc["kind"] == "etl"
    assert doc["cdm"] == "pcornet_7_0"
    assert doc["trigger"]["type"] == "watch"
    # Storage resolved into address names (not raw connections).
    assert doc["input_address"]
    assert doc["output_address"]


@pytest.mark.unit
def test_create_requires_input_and_output_storage(
    web_client, isolated_xdg: Path, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    r = web_client.post(
        "/pipelines",
        data={
            "name": "nostorage",
            "kind": "etl",
            "template": "pcornet_etl",
            "trigger_type": "watch",
        },
        follow_redirects=False,
    )
    # The form is re-rendered in place (not redirected) so the operator's
    # input survives; the error is surfaced on the page.
    assert r.status_code == 400
    body = r.text
    assert "storage account" in body
    # Submitted values are preserved.
    assert 'value="nostorage"' in body
    assert 'value="pcornet_etl"' in body  # chosen template stays checked


@pytest.mark.unit
def test_create_etl_requires_template(
    web_client, isolated_xdg: Path, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    r = web_client.post(
        "/pipelines",
        data={"name": "notemplate", "kind": "etl", "trigger_type": "manual"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    body = r.text
    assert "template" in body
    # The name the operator typed is preserved on the re-rendered form.
    assert 'value="notemplate"' in body
    # The manual trigger they picked stays selected.
    assert 'value="manual" selected' in body


@pytest.mark.unit
def test_edit_form_exposes_watch_trigger(
    web_client, isolated_xdg: Path, tmp_path: Path, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "disc_data2"
    root.mkdir()
    _write_disc_connection(isolated_xdg, "d2", str(root))
    web_client.post(
        "/pipelines",
        data={
            "name": "editme",
            "kind": "etl",
            "template": "pcornet_etl",
            "input_connection": "d2",
            "output_connection": "d2",
            "output_location": "out",
            "trigger_type": "watch",
        },
        follow_redirects=False,
    )
    r = web_client.get("/pipelines/editme/edit")
    assert r.status_code == 200
    body = r.text
    assert 'value="watch"' in body
    assert 'name="watch_min_wait_minutes"' in body
    # Package field is read-only, submitted as a hidden field.
    assert 'id="pipeline"' in body
    assert 'readonly' in body


@pytest.mark.unit
def test_run_specific_batch_passes_batch_id(
    web_client, isolated_xdg: Path, tmp_path: Path, temp_pipelines_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "disc_data3"
    root.mkdir()
    _write_disc_connection(isolated_xdg, "d3", str(root))
    web_client.post(
        "/pipelines",
        data={
            "name": "runbatch",
            "kind": "etl",
            "template": "pcornet_etl",
            "input_connection": "d3",
            "output_connection": "d3",
            "output_location": "out",
            "trigger_type": "manual",
        },
        follow_redirects=False,
    )

    captured: dict[str, object] = {}

    from ovid2.web.services import pipelines_svc

    class _Resp:
        job_id = "job-x"
        instance_id = "inst-x"

    def _fake_run_now(
        name: str,
        *,
        actor: str = "web",
        batch_id=None,
        input_address=None,
        output_address=None,
        tables=None,
    ):  # type: ignore[no-untyped-def]
        captured["name"] = name
        captured["batch_id"] = batch_id
        return _Resp()

    monkeypatch.setattr(pipelines_svc, "run_now", _fake_run_now)

    r = web_client.post(
        "/pipelines/runbatch/run",
        data={"batch_id": "2026_02"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert captured["name"] == "runbatch"
    assert captured["batch_id"] == "2026_02"


@pytest.mark.unit
def test_create_health_pipeline_without_package(
    web_client, isolated_xdg: Path, tmp_path: Path, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    """A health pipeline is package-free: no scaffold, no installed package.

    End-to-end reproduction of the reported failure — creating an
    EDC/health-report pipeline named ``EDC_repot`` used to bounce back
    to ``/pipelines/new?error=Pipeline 'EDC_repot' is not installed
    under ovid2_pipelines.``. It must now 303 to the pipeline page.
    """
    root = tmp_path / "disc_health"
    (root / "2026_01").mkdir(parents=True)
    _write_disc_connection(isolated_xdg, "hd", str(root))

    r = web_client.post(
        "/pipelines",
        data={
            "name": "EDC_repot",
            "kind": "health",
            "input_connection": "hd",
            "input_location": "",
            "output_connection": "hd",
            "output_location": "out",
            "engine": "pyspark",
            "trigger_type": "watch",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/pipelines/EDC_repot", r.headers["location"]

    # No package was scaffolded for a package-free kind.
    assert not (temp_pipelines_root / "EDC_repot").exists()

    # The config was written binding pipeline == name, kind == health.
    cfg_path = (
        isolated_xdg / "config" / "ovid2" / "pipelines" / "EDC_repot.json"
    )
    assert cfg_path.is_file()
    doc = json.loads(cfg_path.read_text())
    assert doc["pipeline"] == "EDC_repot"
    assert doc["kind"] == "health"


@pytest.mark.unit
def test_create_error_preserves_all_entered_values(
    web_client, isolated_xdg: Path, tmp_path: Path, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    """A validation failure re-renders the form with every field intact.

    Regression for the reported UX complaint: adding a pipeline that
    errors used to redirect to a blank ``/pipelines/new`` and lose
    everything the operator typed. It must now re-render (HTTP 400) with
    name / kind / cdm / engine / trigger AND the browsed storage
    accounts + folders re-seeded so nothing is retyped.
    """
    root = tmp_path / "disc_preserve"
    (root / "2026_01").mkdir(parents=True)
    _write_disc_connection(isolated_xdg, "keepme", str(root))

    # Missing template for an ETL kind is a validation error — but the
    # operator also filled in storage, engine, and a cron trigger.
    r = web_client.post(
        "/pipelines",
        data={
            "name": "keepall",
            "kind": "etl",
            "cdm": "",
            "input_connection": "keepme",
            "input_location": "in/batches",
            "output_connection": "keepme",
            "output_location": "out",
            "engine": "snowpark",
            "trigger_type": "cron",
            "cron": "0 3 * * *",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    body = r.text
    # Plain fields.
    assert 'value="keepall"' in body
    assert 'value="snowpark"' in body
    # Trigger type + its sub-field.
    assert 'value="cron" selected' in body
    assert 'value="0 3 * * *"' in body
    # Storage: hidden fields seeded so browse.js can restore the chip.
    assert 'name="input_connection" value="keepme"' in body
    assert 'name="input_location" value="in/batches"' in body
    assert 'name="output_connection" value="keepme"' in body
    assert 'name="output_location" value="out"' in body
    # …and the picker root carries the data-initial-* hints for browse.js.
    assert 'data-initial-connection="keepme"' in body
    assert 'data-initial-location="in/batches"' in body


@pytest.mark.unit
def test_dq_checks_pipeline_runs_edc_cycle_package(
    web_client, isolated_xdg: Path, tmp_path: Path, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    """dq_checks is a package-free EDC report-cycle pipeline.

    Its 'type' is the report cycle (e.g. ``20r1``) chosen on the form,
    persisted as ``packages``. ``pipeline`` binds to the config ``name``
    (a label) — no installed pipeline package is required.
    """
    root = tmp_path / "disc_dq"
    (root / "2026_01").mkdir(parents=True)
    _write_disc_connection(isolated_xdg, "dqd", str(root))

    r = web_client.post(
        "/pipelines",
        data={
            "name": "edc_acme",
            "kind": "dq_checks",
            "packages": "20r1",
            "input_connection": "dqd",
            "input_location": "",
            "output_connection": "dqd",
            "output_location": "out",
            "engine": "pyspark",
            "trigger_type": "watch",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/pipelines/edc_acme", r.headers["location"]

    cfg_path = (
        isolated_xdg / "config" / "ovid2" / "pipelines" / "edc_acme.json"
    )
    assert cfg_path.is_file()
    doc = json.loads(cfg_path.read_text())
    assert doc["kind"] == "dq_checks"
    # pipeline is bound to the config name (label), not an installed pkg.
    assert doc["pipeline"] == "edc_acme"
    # The chosen report cycle is persisted on the config.
    assert doc["packages"] == ["20r1"]


@pytest.mark.unit
def test_dq_checks_rejects_unknown_cycle_package(
    web_client, isolated_xdg: Path, tmp_path: Path, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    """An unknown report-cycle id is rejected with a clear error."""
    root = tmp_path / "disc_dq_bad"
    (root / "2026_01").mkdir(parents=True)
    _write_disc_connection(isolated_xdg, "dqbad", str(root))
    r = web_client.post(
        "/pipelines",
        data={
            "name": "edc_bad",
            "kind": "dq_checks",
            "packages": "99z9",
            "input_connection": "dqbad",
            "output_connection": "dqbad",
            "output_location": "out",
            "trigger_type": "manual",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    body = r.text
    assert "99z9" in body or "report-cycle" in body
    # Submitted values survive the failed submit.
    assert 'value="edc_bad"' in body
    # The kind <select> renders registry labels with a description
    # tooltip, so the dq_checks option carries a ``title`` between the
    # value and ``selected``.
    assert 'value="dq_checks" title=' in body and "selected" in body


@pytest.mark.unit
def test_dq_checks_requires_cycle_choice(
    web_client, isolated_xdg: Path, tmp_path: Path, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    """Omitting the report cycle for dq_checks is a clear error."""
    root = tmp_path / "disc_dq2"
    root.mkdir()
    _write_disc_connection(isolated_xdg, "dqd2", str(root))
    r = web_client.post(
        "/pipelines",
        data={
            "name": "dc_checks_nopkg",
            "kind": "dq_checks",
            "input_connection": "dqd2",
            "output_connection": "dqd2",
            "output_location": "out",
            "trigger_type": "manual",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    body = r.text
    assert "cycle" in body.lower()
    # The label and kind the operator chose survive the failed submit.
    assert 'value="dc_checks_nopkg"' in body
    assert 'value="dq_checks" title=' in body and "selected" in body


@pytest.mark.unit
def test_new_form_lists_cycle_packages_for_dq_checks(
    web_client, isolated_xdg: Path, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    """The create form exposes the EDC report-cycle picker + registered cycles."""
    r = web_client.get("/pipelines/new")
    assert r.status_code == 200
    body = r.text
    assert 'id="cycle-block"' in body
    assert 'name="packages"' in body
    # The baseline cycle package ships with the repo.
    assert "20r1" in body


@pytest.mark.unit
def test_analysis_still_lists_installed_packages(
    web_client, isolated_xdg: Path, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    """analysis still runs over an installed package (unchanged)."""
    (temp_pipelines_root / "pick_me" / "formatters").mkdir(parents=True)
    (temp_pipelines_root / "pick_me" / "__init__.py").write_text("", encoding="utf-8")
    r = web_client.get("/pipelines/new")
    assert r.status_code == 200
    body = r.text
    assert 'id="package-block"' in body
    assert 'name="package"' in body
    assert "pick_me" in body


@pytest.mark.unit
def test_pipelines_list_drops_package_column(
    web_client, isolated_xdg: Path, tmp_path: Path, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "disc_data4"
    root.mkdir()
    _write_disc_connection(isolated_xdg, "d4", str(root))
    web_client.post(
        "/pipelines",
        data={
            "name": "listme",
            "kind": "etl",
            "template": "pcornet_etl",
            "input_connection": "d4",
            "output_connection": "d4",
            "output_location": "out",
            "trigger_type": "manual",
        },
        follow_redirects=False,
    )
    r = web_client.get("/pipelines")
    assert r.status_code == 200
    # The dedicated package column header is gone; the per-row Run
    # control (icon button that opens the folder/schema navigator modal)
    # is present.
    assert "<th>Pipeline</th>" not in r.text
    assert 'data-run-pipeline="listme"' in r.text


# ---------------------------------------------------------------------------
# Open storage — keep input/output unbound at create time, choose at run time.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_new_form_exposes_open_storage_radios(web_client) -> None:  # type: ignore[no-untyped-def]
    """Each storage side has a bind-now / keep-open radio menu."""
    r = web_client.get("/pipelines/new")
    assert r.status_code == 200
    body = r.text
    assert body.count('name="input_open"') == 2
    assert body.count('name="output_open"') == 2
    assert "Keep open" in body
    assert 'data-storage-toggle="input"' in body
    assert 'data-storage-toggle="output"' in body


@pytest.mark.unit
def test_create_with_both_sides_open_stores_empty_addresses(
    web_client, isolated_xdg: Path, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    """A both-open pipeline needs no storage account and stores empty addresses."""
    r = web_client.post(
        "/pipelines",
        data={
            "name": "openboth",
            "kind": "health",
            "input_open": "1",
            "output_open": "1",
            "engine": "pyspark",
            "trigger_type": "manual",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/pipelines/openboth"
    cfg = json.loads(
        (isolated_xdg / "config" / "ovid2" / "pipelines" / "openboth.json").read_text()
    )
    assert cfg["input_address"] == ""
    assert cfg["output_address"] == ""


@pytest.mark.unit
def test_create_bound_side_still_required_when_not_open(
    web_client, isolated_xdg: Path, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    """A side that is NOT kept open still requires a storage account."""
    r = web_client.post(
        "/pipelines",
        data={
            "name": "halfopen",
            "kind": "health",
            "input_open": "0",  # bound — but no connection supplied
            "output_open": "1",
            "engine": "pyspark",
            "trigger_type": "manual",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "keep input open" in r.text.lower()


@pytest.mark.unit
def test_open_input_rejects_watch_trigger(
    web_client, isolated_xdg: Path, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    """watch needs a bound input to enumerate batch folders; open input is rejected."""
    r = web_client.post(
        "/pipelines",
        data={
            "name": "watchopen",
            "kind": "health",
            "input_open": "1",
            "output_open": "1",
            "engine": "pyspark",
            "trigger_type": "watch",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "open" in r.text.lower()
    assert "watch" in r.text.lower()


@pytest.mark.unit
def test_open_pipeline_detail_shows_open_and_choose_run(
    web_client, isolated_xdg: Path, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    """The detail page marks an open side and offers 'choose storage & run'."""
    web_client.post(
        "/pipelines",
        data={
            "name": "opendetail",
            "kind": "health",
            "input_open": "1",
            "output_open": "1",
            "engine": "pyspark",
            "trigger_type": "manual",
        },
        follow_redirects=False,
    )
    r = web_client.get("/pipelines/opendetail")
    assert r.status_code == 200
    assert "chosen at run time" in r.text
    assert "/submit?pipeline=opendetail" in r.text


@pytest.mark.unit
def test_run_now_on_open_pipeline_redirects_to_submit(
    web_client, isolated_xdg: Path, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    """'run now' with no inline storage pick falls back to the Submit form."""
    web_client.post(
        "/pipelines",
        data={
            "name": "openrun",
            "kind": "health",
            "input_open": "1",
            "output_open": "1",
            "engine": "pyspark",
            "trigger_type": "manual",
        },
        follow_redirects=False,
    )
    r = web_client.post(
        "/pipelines/openrun/run",
        data={"batch_id": "20260101"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/submit?")
    assert "pipeline=openrun" in loc
    assert "batch_id=20260101" in loc


@pytest.mark.unit
def test_run_now_open_pipeline_with_inline_storage_runs_immediately(
    web_client, isolated_xdg: Path, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    """Picking storage accounts inline binds the open sides and runs — no /submit."""
    _write_disc_connection(isolated_xdg, "in_store", str(isolated_xdg / "in"))
    _write_disc_connection(isolated_xdg, "out_store", str(isolated_xdg / "out"))
    web_client.post(
        "/pipelines",
        data={
            "name": "openinline",
            "kind": "health",
            "input_open": "1",
            "output_open": "1",
            "engine": "pyspark",
            "trigger_type": "manual",
        },
        follow_redirects=False,
    )
    r = web_client.post(
        "/pipelines/openinline/run",
        data={
            "input_connection": "in_store",
            "output_connection": "out_store",
            "batch_id": "20260101",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    assert loc.startswith("/runs/"), loc
    assert "/submit" not in loc


@pytest.mark.unit
def test_run_now_open_pipeline_derives_batch_id_from_browsed_location(
    web_client, isolated_xdg: Path, temp_pipelines_root: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """No typed batch_id: the browsed folder/schema leaf names the batch.

    The run modal has no free-text batch field — the operator navigates
    to a folder (filesystem) or schema (warehouse), and its final path
    segment becomes the batch id. Input side wins over output.
    """
    _write_disc_connection(isolated_xdg, "in_store", str(isolated_xdg / "in"))
    _write_disc_connection(isolated_xdg, "out_store", str(isolated_xdg / "out"))
    web_client.post(
        "/pipelines",
        data={
            "name": "openderive",
            "kind": "health",
            "input_open": "1",
            "output_open": "1",
            "engine": "pyspark",
            "trigger_type": "manual",
        },
        follow_redirects=False,
    )

    captured: dict[str, object] = {}

    from ovid2.web.services import pipelines_svc

    class _Resp:
        job_id = "job-y"
        instance_id = "inst-y"

    def _fake_run_now(
        name: str,
        *,
        actor: str = "web",
        batch_id=None,
        input_address=None,
        output_address=None,
        tables=None,
    ):  # type: ignore[no-untyped-def]
        captured["batch_id"] = batch_id
        return _Resp()

    monkeypatch.setattr(pipelines_svc, "run_now", _fake_run_now)

    r = web_client.post(
        "/pipelines/openderive/run",
        data={
            "input_connection": "in_store",
            "input_location": "landing/2026_03",
            "output_connection": "out_store",
            "output_location": "warehouse/omop",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert r.headers["location"].startswith("/runs/")
    # Input side wins; the final segment of the browsed path names the batch.
    assert captured["batch_id"] == "2026_03"


@pytest.mark.unit
def test_pipelines_page_lists_storage_accounts_for_open_rows(
    web_client, isolated_xdg: Path, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    """An open row renders an inline storage-account <select> on /pipelines."""
    _write_disc_connection(isolated_xdg, "in_store", str(isolated_xdg / "in"))
    web_client.post(
        "/pipelines",
        data={
            "name": "openlisted",
            "kind": "health",
            "input_open": "1",
            "output_open": "1",
            "engine": "pyspark",
            "trigger_type": "manual",
        },
        follow_redirects=False,
    )
    r = web_client.get("/pipelines")
    assert r.status_code == 200
    body = r.text
    assert 'name="input_connection"' in body
    assert 'name="output_connection"' in body
    assert "in_store" in body



@pytest.mark.unit
def test_submit_form_preselects_pipeline_and_batch(web_client) -> None:  # type: ignore[no-untyped-def]
    """The Submit GET honours ?pipeline= / ?batch_id= preselect params."""
    r = web_client.get("/submit?pipeline=openrun&batch_id=20260101&engine=snowpark")
    assert r.status_code == 200
    body = r.text
    # The value is injected as a selected option even if it is not an
    # installed package / generic label.
    assert '<option value="openrun" selected>' in body
    assert 'value="20260101"' in body
    assert 'value="snowpark"' in body


# ---------------------------------------------------------------------------
# Copy / move — the wizard must be able to CREATE these (regression). Their
# tables cannot use ``all``; they are either listed explicitly or kept open
# (chosen on /submit at run time).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_new_form_exposes_copy_tables_card(web_client) -> None:  # type: ignore[no-untyped-def]
    """The builder wizard renders a copy/move Tables card + list/open radios."""
    r = web_client.get("/pipelines/new")
    assert r.status_code == 200
    body = r.text
    assert 'id="tables-block"' in body
    assert 'name="tables_open" value="0"' in body
    assert 'name="tables_open" value="1"' in body
    assert 'name="tables"' in body
    assert 'data-tables-toggle' in body


@pytest.mark.unit
def test_create_copy_pipeline_with_open_tables(
    web_client, isolated_xdg: Path, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    """Regression: creating a copy pipeline used to 400 with the

    ``copy pipelines require an explicit tables list; ['all'] is not
    supported`` error because the wizard sent no tables and the payload
    defaulted to ``['all']``. Keeping tables open must now create the
    config with an empty tables list (chosen on /submit at run time).
    """
    r = web_client.post(
        "/pipelines",
        data={
            "name": "copyopen",
            "kind": "copy",
            "input_open": "1",
            "output_open": "1",
            "tables_open": "1",
            "engine": "pyspark",
            "trigger_type": "manual",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/pipelines/copyopen"
    cfg = json.loads(
        (isolated_xdg / "config" / "ovid2" / "pipelines" / "copyopen.json").read_text()
    )
    assert cfg["kind"] == "copy"
    assert cfg["tables"] == []


@pytest.mark.unit
def test_create_copy_pipeline_with_explicit_tables(
    web_client, isolated_xdg: Path, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    """Listing tables explicitly stores exactly those tables (``all`` dropped)."""
    r = web_client.post(
        "/pipelines",
        data={
            "name": "copytables",
            "kind": "copy",
            "input_open": "1",
            "output_open": "1",
            "tables_open": "0",
            "tables": "demographic encounter all diagnosis",
            "engine": "pyspark",
            "trigger_type": "manual",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    cfg = json.loads(
        (isolated_xdg / "config" / "ovid2" / "pipelines" / "copytables.json").read_text()
    )
    assert cfg["kind"] == "copy"
    # The stray ``all`` token is dropped; order preserved otherwise.
    assert cfg["tables"] == ["demographic", "encounter", "diagnosis"]


@pytest.mark.unit
def test_create_move_pipeline_with_open_tables(
    web_client, isolated_xdg: Path, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    """move shares copy's shape rules — open tables create a valid config."""
    r = web_client.post(
        "/pipelines",
        data={
            "name": "moveopen",
            "kind": "move",
            "input_open": "1",
            "output_open": "1",
            "tables_open": "1",
            "engine": "pyspark",
            "trigger_type": "manual",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    cfg = json.loads(
        (isolated_xdg / "config" / "ovid2" / "pipelines" / "moveopen.json").read_text()
    )
    assert cfg["kind"] == "move"
    assert cfg["tables"] == []


@pytest.mark.unit
def test_run_modal_tables_field_makes_open_copy_runnable(
    web_client, isolated_xdg: Path, temp_pipelines_root: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """The Run modal's ``tables`` field lets an open copy pipeline run.

    Regression for the field report: a ``copy`` pipeline created *open* on
    tables, run from the Pipelines modal, previously 400'd with
    ``copy jobs require an explicit tables list; ['all'] is not
    supported``. With the modal now posting a ``tables`` list the run
    reaches submit and the explicit list is forwarded verbatim (the stray
    ``all`` token dropped).
    """
    _write_disc_connection(isolated_xdg, "in_store", str(isolated_xdg / "in"))
    _write_disc_connection(isolated_xdg, "out_store", str(isolated_xdg / "out"))
    web_client.post(
        "/pipelines",
        data={
            "name": "copyrun",
            "kind": "copy",
            "input_open": "1",
            "output_open": "1",
            "tables_open": "1",
            "engine": "pyspark",
            "trigger_type": "manual",
        },
        follow_redirects=False,
    )

    captured: dict[str, object] = {}
    from ovid2.web.services import pipelines_svc

    class _Resp:
        job_id = "job-c"
        instance_id = "inst-c"

    def _fake_run_now(
        name: str,
        *,
        actor: str = "web",
        batch_id=None,
        input_address=None,
        output_address=None,
        tables=None,
    ):  # type: ignore[no-untyped-def]
        captured["tables"] = tables
        return _Resp()

    monkeypatch.setattr(pipelines_svc, "run_now", _fake_run_now)

    r = web_client.post(
        "/pipelines/copyrun/run",
        data={
            "input_connection": "in_store",
            "output_connection": "out_store",
            "batch_id": "20260101",
            "tables": "DEMOGRAPHIC ENCOUNTER all",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert r.headers["location"].startswith("/runs/")
    # The stray ``all`` token is dropped; the explicit list reaches run_now.
    assert captured["tables"] == ["DEMOGRAPHIC", "ENCOUNTER"]


@pytest.mark.unit
def test_run_modal_shows_tables_field_for_copy_only(
    web_client, isolated_xdg: Path, temp_pipelines_root: Path
) -> None:  # type: ignore[no-untyped-def]
    """The Pipelines page flags copy/move rows as needing a tables list."""
    _write_disc_connection(isolated_xdg, "in_store", str(isolated_xdg / "in"))
    _write_disc_connection(isolated_xdg, "out_store", str(isolated_xdg / "out"))
    web_client.post(
        "/pipelines",
        data={
            "name": "copyflag",
            "kind": "copy",
            "input_open": "1",
            "output_open": "1",
            "tables_open": "1",
            "engine": "pyspark",
            "trigger_type": "manual",
        },
        follow_redirects=False,
    )
    r = web_client.get("/pipelines")
    assert r.status_code == 200
    body = r.text
    # The copy row's Run button advertises the tables requirement, and the
    # modal carries a tables input.
    assert 'data-tables-required="true"' in body
    assert "data-run-tables-input" in body



