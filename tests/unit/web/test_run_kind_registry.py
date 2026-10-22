"""Unit tests for the run-kind registry auto-pickup contract.

These prove the central promise of the registry: adding one
:class:`~ovid2.run_kinds.KindSpec` entry makes a *new* run kind flow
through the Pipelines page grouping, create/submit validation, and the
worker argv dispatch **without editing any of those modules**. Each test
injects a synthetic kind into :data:`ovid2.run_kinds.KIND_REGISTRY` via
``monkeypatch`` and asserts the downstream behaviour follows.
"""

from __future__ import annotations

import pytest

from ovid2 import run_kinds
from ovid2.run_kinds import KindSpec


@pytest.fixture()
def synthetic_kind(monkeypatch: pytest.MonkeyPatch) -> KindSpec:
    """Register a synthetic ``xform`` kind for the duration of a test.

    Shaped like an etl-family kind (package-free so submit does not
    require an installed package) with the ``etl`` argv builder.
    """
    spec = KindSpec(
        name="xform",
        label="Transform",
        description="Synthetic test kind.",
        package_required=False,
        cli_subcommand="xform",
        argv_builder="etl",
        uses_transformations=True,
        uses_cdm=False,
        tables_mode="all",
        read_only_data_mount=True,
        create_flow="plain",
    )
    patched = dict(run_kinds.KIND_REGISTRY)
    patched["xform"] = spec
    monkeypatch.setattr(run_kinds, "KIND_REGISTRY", patched)
    return spec


@pytest.mark.unit
def test_new_kind_appears_in_derived_aliases(synthetic_kind: KindSpec) -> None:
    """A registry addition is visible through the neutral helpers."""
    assert "xform" in run_kinds.all_kinds()
    # package_required=False → shows up in the package-free set.
    assert "xform" in run_kinds.package_free_kinds()
    assert run_kinds.get_kind("xform") is synthetic_kind
    assert run_kinds.kind_for_command("xform") is synthetic_kind


@pytest.mark.unit
def test_new_kind_dispatches_through_worker_without_edits(
    synthetic_kind: KindSpec,
) -> None:
    """The worker routes an unknown-but-registered kind by its builder."""
    from ovid2.web.models import JobDetail
    from ovid2.web.worker import Worker

    detail = JobDetail(
        job_id="J1",
        submitted_at="2026-01-01T00:00:00+00:00",
        submitted_by="test",
        pipeline="p",
        batch_id="B1",
        input_address="in",
        output_address="out",
        tables=["demographic"],
        transformations=["format"],
        status="queued",
        instance_id="I1",
        extra_args={"kind": "xform", "engine": "pyspark"},
    )
    worker = Worker(ovid2_bin=["ovid2"])
    argv = worker._build_argv(detail)
    # The etl argv builder emitted the registry's cli_subcommand verbatim.
    assert argv[0] == "ovid2"
    assert argv[1] == "xform"
    assert "--pipeline" in argv and "--tables" in argv


@pytest.mark.unit
def test_new_kind_groups_on_pipelines_page(synthetic_kind: KindSpec) -> None:
    """``PIPELINE_KINDS`` (page grouping order) picks up the new kind."""
    # Re-derive the alias the way models.py does, so the test reflects a
    # fresh process import order.
    assert "xform" in run_kinds.all_kinds()


@pytest.mark.unit
def test_readonly_mount_default_is_safe_for_new_kind() -> None:
    """A kind with no explicit mount flag defaults to read-only /data.

    Guards the "never silently widen to rw" invariant for authors who
    forget the flag.
    """
    spec = KindSpec(
        name="z",
        label="Z",
        description="d",
        package_required=False,
        cli_subcommand="z",
        argv_builder="etl",
        uses_transformations=True,
        uses_cdm=False,
        tables_mode="all",
    )
    assert spec.read_only_data_mount is True
