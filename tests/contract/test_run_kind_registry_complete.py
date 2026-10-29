"""Contract: the run-kind registry is complete and self-consistent.

The registry at :mod:`ovid2.run_kinds` is the single source of truth for
pipeline run kinds. This contract locks the invariants that let a new
kind be "picked up automatically" once added:

1. Every :class:`~ovid2.run_kinds.KindSpec` resolves to a real argv
   builder and a non-empty CLI subcommand.
2. The derived aliases in :mod:`ovid2.web.models`
   (``PIPELINE_KINDS`` / ``PACKAGE_FREE_KINDS``) match the registry
   exactly — no drift between the neutral registry and the web view.
3. Every kind the worker can dispatch has a matching argv-builder
   method on the worker, and every host-side data-mount decision
   resolves through :func:`~ovid2.run_kinds.kind_for_command`.

Widening the registry is deliberate: the frozen tripwire in
``test_route_kind_valid.py`` also fires, forcing an ADR + test update.
"""

from __future__ import annotations

import pytest

from ovid2.run_kinds import (
    KIND_REGISTRY,
    all_kinds,
    get_kind,
    kind_for_command,
    normalize_kind,
    package_free_kinds,
)

_VALID_ARGV_BUILDERS = {"etl", "health", "copy", "move", "export"}
_VALID_TABLES_MODES = {"all", "explicit", "none"}
_VALID_CREATE_FLOWS = {
    "scaffold_package",
    "pick_package",
    "cycle_packages",
    "plain",
}


@pytest.mark.contract
def test_every_spec_is_internally_consistent() -> None:
    """Each KindSpec has a valid builder / subcommand / tables mode / flow."""
    offenders: list[str] = []
    for name, spec in KIND_REGISTRY.items():
        if spec.name != name:
            offenders.append(f"{name}: spec.name={spec.name!r} != key")
        if not spec.cli_subcommand.strip():
            offenders.append(f"{name}: empty cli_subcommand")
        if spec.argv_builder not in _VALID_ARGV_BUILDERS:
            offenders.append(f"{name}: bad argv_builder {spec.argv_builder!r}")
        if spec.tables_mode not in _VALID_TABLES_MODES:
            offenders.append(f"{name}: bad tables_mode {spec.tables_mode!r}")
        if spec.create_flow not in _VALID_CREATE_FLOWS:
            offenders.append(f"{name}: bad create_flow {spec.create_flow!r}")
        if not spec.label.strip() or not spec.description.strip():
            offenders.append(f"{name}: missing label/description")
    assert not offenders, "KindSpec inconsistencies:\n  " + "\n  ".join(offenders)


@pytest.mark.contract
def test_web_aliases_match_registry() -> None:
    """``PIPELINE_KINDS`` / ``PACKAGE_FREE_KINDS`` are exact registry views."""
    from ovid2.web.models import PACKAGE_FREE_KINDS, PIPELINE_KINDS

    assert tuple(PIPELINE_KINDS) == all_kinds()
    assert package_free_kinds() == PACKAGE_FREE_KINDS


@pytest.mark.contract
def test_worker_has_a_builder_for_every_kind() -> None:
    """Every kind's ``argv_builder`` maps to a real worker method."""
    from ovid2.web.worker import Worker

    method_for_builder = {
        "etl": "_build_argv_etl",
        "health": "_build_argv_health",
        "copy": "_build_argv_copy",
        "move": "_build_argv_move",
        "export": "_build_argv_export",
    }
    offenders: list[str] = []
    for name, spec in KIND_REGISTRY.items():
        method = method_for_builder.get(spec.argv_builder)
        if method is None or not hasattr(Worker, method):
            offenders.append(f"{name}: no worker method for {spec.argv_builder!r}")
    assert not offenders, "Missing worker argv builders:\n  " + "\n  ".join(offenders)


@pytest.mark.contract
def test_kind_for_command_resolves_both_forms() -> None:
    """``kind_for_command`` resolves both the bare name and the subcommand."""
    for name, spec in KIND_REGISTRY.items():
        assert kind_for_command(name) is spec, f"bare name {name!r}"
        assert kind_for_command(spec.cli_subcommand) is spec, spec.cli_subcommand
    # Unknown command falls through to None (callers default to etl shape).
    assert kind_for_command("does-not-exist") is None
    assert kind_for_command(None) is None


@pytest.mark.contract
def test_normalize_and_get_kind_round_trip() -> None:
    """``get_kind`` accepts aliases via ``normalize_kind``."""
    assert normalize_kind("cdm") == "etl"
    assert get_kind("cdm") is KIND_REGISTRY["etl"]
    assert get_kind(None) is KIND_REGISTRY["etl"]
    assert get_kind("unknown-kind") is None


@pytest.mark.contract
def test_data_mount_flags_match_historical_behaviour() -> None:
    """The read-only ``/data`` mount set matches the pre-refactor rule.

    Historically ``build_docker_command`` mounted ``/data`` read-write
    for copy / move / health and read-only for etl / analysis /
    dq_checks. Lock that mapping so a registry edit that would silently
    widen the read-write set trips here.
    """
    ro = {n for n, s in KIND_REGISTRY.items() if s.read_only_data_mount}
    rw = {n for n, s in KIND_REGISTRY.items() if not s.read_only_data_mount}
    # ``export`` is a host-side executor subprocess (no container); the
    # flag is the conservative default.
    assert ro == {"etl", "analysis", "dq_checks", "export"}
    assert rw == {"copy", "move", "health"}


@pytest.mark.contract
def test_job_only_kinds_are_not_pipeline_kinds() -> None:
    """Job-only kinds (``export``) are dispatchable but never offered as pipelines."""
    from ovid2.web.kinds import kind_choices

    job_only = {n for n, s in KIND_REGISTRY.items() if s.job_only}
    assert job_only == {"export"}
    assert not job_only & set(all_kinds())
    assert not job_only & {k["name"] for k in kind_choices()}
    for name in job_only:
        assert get_kind(name) is KIND_REGISTRY[name]
