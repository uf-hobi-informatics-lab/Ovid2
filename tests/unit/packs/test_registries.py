"""Unit tests — active domain packs feed every registry.

A folder pack (YAML-only) contributes a CDM, health checks, packages,
vocabularies, bindings and report templates; an installed (pip) pack
additionally contributes check operators and fixers. Each test enables
the pack, clears the relevant caches and reads through the *real*
registry / catalogue the executor and the web tier use.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from ovid2.packs import clear_cache
from tests.unit.packs.conftest import make_pack, write_yaml

pytestmark = pytest.mark.unit


@pytest.fixture()
def fresh_registries() -> Iterator[None]:
    """Clear the check registry / template caches around a test."""
    from ovid2.reports import catalogue
    from ovid2.transformations.quality import registry

    registry.clear_cache()
    catalogue._templates.cache_clear()
    yield
    clear_cache()
    registry.clear_cache()
    catalogue._templates.cache_clear()


@pytest.fixture()
def acme(pack_env: Path, monkeypatch: pytest.MonkeyPatch, fresh_registries: None) -> Path:
    """An enabled folder pack ``acme`` with a CDM, 2 checks and a package."""
    pack = make_pack(
        pack_env,
        "acme",
        provides={
            "cdms": ["acme"],
            "checks_dir": "checks",
            "packages_dir": "packages",
            "vocabularies_dir": "vocab",
            "report_templates_dir": "templates",
            "parts": [{"id": "acme_safety", "label": "Safety"}],
        },
        checks=[
            {
                "id": "acme.subject_present",
                "name": "SUBJECT_PRESENT",
                "part": "part1",
                "description": "SUBJECT is present",
                "operator": "table_present",
                "source_tables": ["SUBJECT"],
                "severity": "blocker",
                "applies_to_cdms": ["acme"],
                "remediation": {"hint": "Deliver SUBJECT.", "transforms": ["standardize_null"]},
            },
            {
                "id": "acme.safety_present",
                "name": "SAFETY_PRESENT",
                "part": "acme_safety",
                "description": "SUBJECT again, in the pack part",
                "operator": "table_present",
                "source_tables": ["SUBJECT"],
                "severity": "info",
            },
        ],
    )
    # One misnamed check: must be skipped (namespace rule).
    write_yaml(
        pack / "checks" / "rogue.yaml",
        {
            "id": "1.01",
            "name": "ROGUE",
            "part": "part1",
            "description": "shadows a core check",
            "operator": "table_present",
            "source_tables": ["SUBJECT"],
        },
    )
    write_yaml(
        pack / "vocab" / "arms.yaml",
        {
            "v": 1,
            "kind": "vocabulary",
            "name": "acme.arms",
            "version": "1",
            "description": "Trial arms",
            "inline_items": [{"code": "A", "description": "Arm A"}],
        },
    )
    write_yaml(
        pack / "vocab" / "rogue.yaml",
        {"v": 1, "kind": "vocabulary", "name": "loinc_rogue", "version": "1", "description": "x"},
    )
    write_yaml(
        pack / "templates" / "health" / "trial_summary.yaml",
        {
            "name": "Trial summary",
            "layout": {
                "v": 1,
                "title": "Trial",
                "sections": [
                    {"id": "s", "title": "S", "blocks": [{"id": "t", "type": "text", "text": "x"}]}
                ],
            },
        },
    )
    monkeypatch.setenv("OVID2_PACKS_ENABLED", "acme")
    clear_cache()
    return pack


def test_pack_cdm_loads_and_registers(acme: Path) -> None:
    from ovid2.cdm.yaml_loader import cdm_yaml_dir, load_cdm

    assert cdm_yaml_dir("acme") == acme / "cdms" / "acme"
    handle = load_cdm("acme")
    assert handle.name == "acme" and [t.lower() for t in handle.tables()] == ["subject"]

    import ovid2.cdm as cdm_pkg
    from ovid2.cdm import registry

    try:
        cdm_pkg._register_pack_cdms()
        assert "acme" in registry.registered_cdms()
    finally:
        registry._REGISTRY.pop("acme", None)


def test_pack_cannot_shadow_core_cdm(pack_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ovid2.cdm.yaml_loader import cdm_yaml_dir, cdm_yaml_root

    make_pack(pack_env, "pcornet_7_0", provides={"cdms": ["pcornet_7_0"]}, checks=[], packages=[])
    monkeypatch.setenv("OVID2_PACKS_ENABLED", "*")
    clear_cache()
    assert cdm_yaml_dir("pcornet_7_0") == cdm_yaml_root() / "pcornet_7_0"


def test_disabled_pack_cdm_is_invisible(pack_env: Path) -> None:
    from ovid2.cdm.yaml_loader import CdmYamlError, cdm_yaml_dir

    make_pack(pack_env, "acme")
    clear_cache()
    with pytest.raises(CdmYamlError):
        cdm_yaml_dir("acme")


def test_check_registry_includes_pack_checks(acme: Path) -> None:
    from ovid2.transformations.quality.registry import (
        get_package,
        get_spec,
        load_all_packages,
        part_order,
        parts,
    )

    spec = get_spec("acme.subject_present")
    assert spec is not None
    assert spec.pack == "acme" and spec.severity == "blocker"
    assert spec.applies_to("acme") and not spec.applies_to("pcornet_7_0")
    assert get_spec("1.01").pack == "pcornet_7_0"  # type: ignore[union-attr]
    assert get_spec("1.01").name != "ROGUE"  # type: ignore[union-attr]
    assert "acme_safety" in parts() and part_order()["acme_safety"] > part_order()["semantic"]
    pkg = get_package("acme.baseline")
    assert pkg is not None and pkg.pack == "acme"
    assert {p.id for p in load_all_packages()} >= {"20r1", "dcq_v7_03", "acme.baseline"}


def test_web_catalogue_matches_registry(acme: Path) -> None:
    from ovid2.transformations.quality.registry import load_all_specs
    from ovid2.web.services import health_report_svc

    web = [c["id"] for c in health_report_svc.list_checks()]
    assert set(web) == {s.id for s in load_all_specs()}
    entry = health_report_svc.check_catalogue()["acme.subject_present"]
    assert entry["pack"] == "acme"
    assert entry["remediation"]["transforms"] == ["standardize_null"]
    assert web.index("acme.safety_present") > web.index("acme.subject_present")
    assert {p["id"]: p["pack"] for p in health_report_svc.list_packages()}[
        "acme.baseline"
    ] == "acme"


def test_runner_marks_other_cdm_checks_not_applicable(acme: Path) -> None:
    from ovid2.cdm.registry import get_cdm
    from ovid2.transformations.quality.registry import get_spec
    from ovid2.transformations.quality.report import build_report
    from ovid2.transformations.quality.runner import HealthCheckRunner, all_candidate_tables

    spec = get_spec("acme.subject_present")
    core = get_spec("1.01")
    assert spec is not None and core is not None
    assert "SUBJECT" not in all_candidate_tables([spec], "pcornet_7_0")
    assert "SUBJECT" in all_candidate_tables([spec], "acme")

    class _Engine:
        session_is_spark = True

        def get_functions(self) -> object:
            return object()

    runner = HealthCheckRunner(
        session=None,
        cdm=get_cdm("pcornet_7_0"),
        pipeline="t",
        batch_id="b",
        instance_id="i",
        engine=_Engine(),  # type: ignore[arg-type]
    )
    outcomes = runner.run([spec], available_tables=frozenset())
    assert outcomes[0].status == "not_applicable"
    assert "applies to acme" in outcomes[0].skip_reason
    assert outcomes[0].pack == "acme" and outcomes[0].severity == "blocker"
    report = build_report(
        instance_id="i",
        pipeline="t",
        batch_id="b",
        cdm="pcornet_7_0",
        input_address="in",
        output_address="out",
        outcomes=outcomes,
    )
    assert report["summary"]["checks_not_applicable"] == 1
    assert report["summary"]["packs"] == {"acme": 1}


def test_pack_vocabularies_are_namespaced(acme: Path) -> None:
    from ovid2.semantic.vocabularies._yaml_loader import iter_vocabulary_docs

    names = [payload["name"] for _stem, payload in iter_vocabulary_docs()]
    assert "acme.arms" in names
    assert "loinc_rogue" not in names


def test_pack_report_templates(acme: Path) -> None:
    from ovid2.reports.catalogue import get_template, list_templates

    tmpl = get_template("acme.health.trial_summary")
    assert tmpl is not None and not tmpl.builtin and tmpl.source_kind == "health"
    assert "acme.health.trial_summary" in {t.template_id for t in list_templates("health")}


# ---------------------------------------------------------------------------
# Installed (pip) packs: operators + fixers
# ---------------------------------------------------------------------------

_OPERATORS = textwrap.dedent(
    '''
    """Operators for the acme pack."""
    from ovid2.transformations.quality.ops import _mk, operator


    @operator("acme_row_flag")
    def _row_flag(ctx, spec):
        """Emit a constant flag."""
        return [_mk(spec, source_table="SUBJECT", metric="FLAG", value_num=1.0)]


    @operator("not_namespaced")
    def _rogue(ctx, spec):
        """Must be dropped."""
        return []
    '''
)

_FIXERS = textwrap.dedent(
    '''
    """Fixers for the acme pack."""
    from ovid2.transformations.fixers.base import BaseFixer


    class AcmeTrim(BaseFixer):
        """Trim whitespace in SUBJECT_ID."""

        @property
        def name(self):
            return "acme_trim"

        def apply(self, df, context):
            return df


    FIXERS = {"acme_trim": AcmeTrim}
    '''
)


@pytest.fixture()
def acme_pip(
    pip_pack: Callable[..., Path], monkeypatch: pytest.MonkeyPatch, fresh_registries: None
) -> Iterator[Path]:
    """An enabled installed pack shipping one operator and one fixer."""
    pkg = pip_pack(
        "acme",
        provides={
            "cdms": ["acme"],
            "checks_dir": "checks",
            "packages_dir": "packages",
            "operators_module": "operators",
            "fixers_module": "fixers",
            "fixers": {"acme_trim": {"summary": "Trim whitespace in SUBJECT_ID.", "scope": "acme"}},
        },
        files={"operators.py": _OPERATORS, "fixers.py": _FIXERS},
    )
    monkeypatch.setenv("OVID2_PACKS_ENABLED", "acme")
    clear_cache()
    yield pkg


def test_pip_pack_operators_load_and_are_namespaced(acme_pip: Path) -> None:
    from ovid2.transformations.quality import ops

    monkeypatch_state = (ops._PACKS_LOADED, dict(ops._OPERATORS), dict(ops.PACK_OPERATOR_ERRORS))
    try:
        ops._PACKS_LOADED = False
        ops.PACK_OPERATOR_ERRORS.clear()
        assert "acme_row_flag" in ops.operator_names()
        assert ops.get_operator("not_namespaced") is None
        assert "must be named acme_<name>" in ops.PACK_OPERATOR_ERRORS["acme"]
    finally:
        ops._PACKS_LOADED, saved_ops, saved_err = monkeypatch_state
        ops._OPERATORS.clear()
        ops._OPERATORS.update(saved_ops)
        ops.PACK_OPERATOR_ERRORS.clear()
        ops.PACK_OPERATOR_ERRORS.update(saved_err)


def test_operator_collision_is_refused() -> None:
    from ovid2.transformations.quality.ops import OperatorCollisionError, operator

    with pytest.raises(OperatorCollisionError, match="table_present"):

        @operator("table_present")
        def _shadow(ctx, spec):  # type: ignore[no-untyped-def]
            return []


def test_pip_pack_fixers_reach_both_catalogues(acme_pip: Path) -> None:
    from ovid2.config import fixers_catalogue as cat
    from ovid2.transformations.fixers.common import registry as reg

    saved = (dict(reg.COMMON_FIXES), dict(reg.FIXER_ALIASES), dict(reg.FIXER_SCOPES))
    saved_cat = (dict(cat.FIXER_CATALOGUE), dict(cat.FIXER_COLUMN_ROLES))
    try:
        reg._load_pack_fixers()
        cat._pack_entries()
        assert reg.COMMON_FIXES["acme_trim"].__name__ == "AcmeTrim"
        assert reg.scope_of("acme_trim") == "acme"
        assert reg.is_compatible_with_cdm("acme_trim", "acme")
        assert not reg.is_compatible_with_cdm("acme_trim", "pcornet_7_0")
        entry = cat.FIXER_CATALOGUE["acme_trim"]
        assert entry["pack"] == "acme" and entry["scope"] == "acme"
        assert entry["summary"] == "Trim whitespace in SUBJECT_ID."
        assert [f["name"] for f in cat.list_fixers("acme") if f["name"] == "acme_trim"]
        assert not [f for f in cat.list_fixers("pcornet_7_0") if f["name"] == "acme_trim"]
    finally:
        for target, value in zip(
            (reg.COMMON_FIXES, reg.FIXER_ALIASES, reg.FIXER_SCOPES), saved, strict=True
        ):
            target.clear()
            target.update(value)
        for target, value in zip(
            (cat.FIXER_CATALOGUE, cat.FIXER_COLUMN_ROLES), saved_cat, strict=True
        ):
            target.clear()
            target.update(value)


def test_multi_cdm_fixer_scope() -> None:
    from ovid2.transformations.fixers.common import registry as reg

    reg.FIXER_SCOPES["_tmp_multi"] = "acme_sdtm, acme_adam"
    try:
        assert reg.is_compatible_with_cdm("_tmp_multi", "acme_adam")
        assert not reg.is_compatible_with_cdm("_tmp_multi", "pcornet_7_0")
    finally:
        del reg.FIXER_SCOPES["_tmp_multi"]
