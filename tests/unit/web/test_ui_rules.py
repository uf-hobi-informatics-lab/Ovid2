"""UI rules U1–U3 — behaviour of the shared pieces.

The static invariants live in ``tests/contract/test_ui_rules.py``; these
tests exercise the server side of them:

- ``kind_choices()`` / the ``kind_spec`` Jinja global carry the flags the
  browser visibility module reads (U1);
- the pipeline editor posts stages / tables as pickers and the route
  flattens them back to the config shape (U1 + U2);
- catalogue ``choices`` fields render as ``<select>`` and field help sits
  under a "?" tip (U2 + U3);
- the read-only pipeline page shows only the rows the kind uses (U1).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.datastructures import FormData


@pytest.mark.unit
def test_kind_flags_cover_registry() -> None:
    """Flags are derived from KindSpec, not hand-written per kind."""
    from ovid2.run_kinds import KIND_REGISTRY
    from ovid2.web.kinds import kind_choices

    by_name = {k["name"]: k for k in kind_choices()}
    for name, spec in KIND_REGISTRY.items():
        if spec.job_only:
            assert name not in by_name
            continue
        k = by_name[name]
        assert k["uses_cdm"] is spec.uses_cdm
        assert k["tables_mode"] == spec.tables_mode
        assert k["is_transfer"] is (spec.argv_builder in ("copy", "move"))
        assert k["uses_engine_overrides"] is (spec.argv_builder in ("etl", "health"))


@pytest.mark.unit
def test_kind_spec_jinja_global() -> None:
    from ovid2.web.templating import kind_spec, templates

    assert templates.env.globals["kind_spec"] is kind_spec
    assert kind_spec("copy")["is_transfer"] is True
    assert kind_spec("cdm")["name"] == "etl"  # alias normalised
    assert kind_spec("nope") == {}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("pairs", "want_tables", "want_tx"),
    [
        (
            [("tables_mode", "all")]
            + [
                ("transformations", s) for s in ("format", "deduplicate", "transform", "map", "fix")
            ],
            "all",
            "all",
        ),
        (
            [
                ("tables_mode", "pick"),
                ("tables_pick", "demographic"),
                ("tables_pick", "vital"),
                ("transformations", "format"),
                ("transformations", "map"),
            ],
            "demographic vital",
            "format map",
        ),
        ([("tables", "a b")], "a b", None),  # copy / move: plain text passes through
    ],
)
def test_flatten_pipeline_edit_form(
    pairs: list[tuple[str, str]], want_tables: str, want_tx: str | None
) -> None:
    from ovid2.web.routes.html import _flatten_pipeline_edit_form

    form = _flatten_pipeline_edit_form(FormData(pairs))
    assert form["tables"] == want_tables
    assert form.get("transformations") == want_tx
    assert "tables_pick" not in form and "tables_mode" not in form


@pytest.mark.unit
def test_flatten_pipeline_edit_form_rejects_empty_pick() -> None:
    from ovid2.web.routes.html import _build_pipeline_write, _flatten_pipeline_edit_form

    form = _flatten_pipeline_edit_form(FormData([("tables_mode", "pick")]))
    with pytest.raises(ValueError, match="at least one table"):
        _build_pipeline_write(form)


def _write(xdg: Path, sub: str, name: str, doc: dict[str, object]) -> None:
    d = xdg / "config" / "ovid2" / sub
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(doc))


@pytest.mark.unit
def test_connection_form_renders_choices_and_tips(web_client) -> None:  # type: ignore[no-untyped-def]
    body = web_client.get("/connections/new?type=postgres").text
    assert '<select id="arg_sslmode" name="arg_sslmode"' in body
    assert '<option value="verify-full"' in body
    # U3: help is a tip, not a "Help" column
    assert "<th>Help</th>" not in body
    assert "help-tip-btn" in body


@pytest.mark.unit
def test_location_form_format_is_select(web_client, isolated_xdg: Path) -> None:  # type: ignore[no-untyped-def]
    _write(isolated_xdg, "connections", "d1", {"type": "disc", "args": {"host_path": "/tmp"}})
    body = web_client.get("/addresses/new/step2?connection=d1").text
    assert '<select id="loc_format" name="loc_format"' in body
    assert '<input type="checkbox" id="loc_lowercase_columns"' in body


@pytest.mark.unit
def test_snowpark_connection_lists_snowflake_connections(  # type: ignore[no-untyped-def]
    web_client, isolated_xdg: Path
) -> None:
    _write(isolated_xdg, "connections", "sf_a", {"type": "snowflake", "args": {"account": "x"}})
    _write(isolated_xdg, "connections", "d1", {"type": "disc", "args": {"host_path": "/tmp"}})
    body = web_client.get("/engines/new?type=snowpark").text
    assert '<select id="arg_connection" name="arg_connection"' in body
    assert '<option value="sf_a"' in body
    assert '<option value="d1"' not in body


@pytest.mark.unit
def test_engine_bool_fields_are_selects(web_client) -> None:  # type: ignore[no-untyped-def]
    body = web_client.get("/engines/new?type=duckdb").text
    assert '<select id="arg_preserve_insertion_order"' in body


@pytest.mark.unit
def test_pipeline_detail_hides_irrelevant_rows(web_client, isolated_xdg: Path) -> None:  # type: ignore[no-untyped-def]
    _write(
        isolated_xdg,
        "pipelines",
        "cp",
        {
            "kind": "copy",
            "pipeline": "cp",
            "input_address": "",
            "output_address": "",
            "tables": ["demographic"],
            "transformations": [],
            "engine": "pyspark",
            "trigger": {"type": "manual"},
        },
    )
    body = web_client.get("/pipelines/cp/config").text
    if "Pipeline:" not in body:  # detail lives at /pipelines/{name} on newer routes
        body = web_client.get("/pipelines/cp").text
    assert "<th>Transformations</th>" not in body
    assert "<th>Workers</th>" not in body
    assert "<th>CDM</th>" not in body


# ---------------------------------------------------------------------------
# U2 — page-level selects
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_settings_page_uses_selects(web_client) -> None:  # type: ignore[no-untyped-def]
    body = web_client.get("/settings").text
    assert '<select id="defaults_engine" name="defaults_engine">' in body
    assert '<select id="ui_tz" name="ui_tz">' in body
    assert '<option value="Europe/Paris"' in body
    assert '<select id="smtp_secret_ref" name="smtp_secret_ref">' in body
    assert "<datalist" not in body


@pytest.mark.unit
def test_runs_filter_is_a_select(web_client) -> None:  # type: ignore[no-untyped-def]
    body = web_client.get("/runs").text
    assert '<select id="run-filter-pipeline" name="pipeline">' in body


@pytest.mark.unit
def test_pipelines_cdm_filter_is_a_select(web_client, isolated_xdg: Path) -> None:  # type: ignore[no-untyped-def]
    _write(
        isolated_xdg,
        "pipelines",
        "cp",
        {
            "kind": "copy",
            "pipeline": "cp",
            "input_address": "",
            "output_address": "",
            "tables": ["demographic"],
            "engine": "pyspark",
            "trigger": {"type": "manual"},
        },
    )
    body = web_client.get("/pipelines").text
    assert '<select id="pf-cdm" data-filter="cdm"' in body
    assert '<option value="pcornet_7_0">' in body


@pytest.mark.unit
def test_inspection_carries_picker_lists() -> None:
    from ovid2.web.services.pipeline_inspect_svc import inspect_package

    d = inspect_package("example_pcornet").to_dict()
    demo = next(t for t in d["tables"] if t["name"] == "demographic")
    assert "PATID" in demo["columns"]
    assert "loinc" in d["vocabularies"]
    assert "custom" in d["formatter_adapters"]
    assert set(d["reference_tables"]) <= {t["name"] for t in d["tables"]}


@pytest.mark.unit
def test_shareable_roles_exclude_inactive() -> None:
    from ovid2.web.routes.html import _shareable_roles

    roles = _shareable_roles()
    assert "viewer" in roles and "system_admin" in roles
    assert "pending" not in roles and "guest" not in roles
