"""Unit tests for the plan-time capability check (``ovid2.config.capability_plan``).

The check runs before any cluster starts (``ovid2 etl``) and before a job is
queued (``jobs_svc.submit``). These tests prove that a step the engine cannot
execute — which used to surface mid-run as ``EngineOperationUnsupported`` —
is now refused at plan time, and that supported plans pass untouched.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ovid2.config.capability_plan import (
    CapabilityPlanError,
    PlannedAlgorithm,
    check_pipeline,
    missing_capabilities,
    planned_algorithms,
)

_ENGINES = ("pyspark", "databricks_spark", "duckdb", "snowpark")


def _make_package(root: Path, name: str, *, assignments: str = "", mapper: str = "") -> None:
    pkg = root / "ovid2_pipelines" / name
    (pkg / "formatters").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "pipeline_settings.py").write_text('cdm = "pcornet_7_0"\n', encoding="utf-8")
    (pkg / "formatters" / "demographic.yaml").write_text(
        "v: 1\nformatter: demographic\nadapter: source_passthrough\n", encoding="utf-8"
    )
    if assignments:
        (pkg / "transforms").mkdir()
        (pkg / "transforms" / "assignments.yaml").write_text(assignments, encoding="utf-8")
    if mapper:
        (pkg / "mappers").mkdir()
        (pkg / "mappers" / "demographic.yaml").write_text(mapper, encoding="utf-8")


@pytest.fixture()
def pkg_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "pkgs"
    root.mkdir()
    import ovid2_pipelines

    monkeypatch.setattr(
        ovid2_pipelines, "__path__", [*ovid2_pipelines.__path__, str(root / "ovid2_pipelines")]
    )
    monkeypatch.syspath_prepend(str(root))
    yield root
    for mod in [m for m in sys.modules if m.startswith("ovid2_pipelines.capplan_")]:
        sys.modules.pop(mod, None)


@pytest.mark.parametrize("engine", _ENGINES)
@pytest.mark.parametrize("pipeline", ["example_pcornet", "pcornet_to_omop"])
def test_shipped_pipelines_plan_on_every_engine(pipeline: str, engine: str) -> None:
    """The site-neutral packages need nothing beyond the portable set."""
    algorithms = check_pipeline(engine, pipeline, ["all"])
    kinds = {a.kind for a in algorithms}
    assert {"formatter", "deduplicator"} <= kinds


def test_seeded_sample_is_refused_on_engines_without_seeds(pkg_root: Path) -> None:
    """``random_sample(seed=…)`` needs ``rand_seeded``: refused on DuckDB / Snowpark."""
    _make_package(
        pkg_root,
        "capplan_seeded",
        assignments=(
            "v: 1\ntables:\n  demographic:\n"
            "    - steps:\n        - {op: random_sample, fraction: 0.5, seed: 7}\n"
        ),
    )
    for engine in ("duckdb", "snowpark"):
        with pytest.raises(CapabilityPlanError) as err:
            check_pipeline(engine, "capplan_seeded", ["demographic"])
        assert "rand_seeded" in str(err.value)
        assert "transform:random_sample" in str(err.value)
        assert err.value.missing == {"rand_seeded": ["transform:random_sample"]}
    for engine in ("pyspark", "databricks_spark"):
        check_pipeline(engine, "capplan_seeded", ["demographic"])


def test_unseeded_sample_runs_everywhere(pkg_root: Path) -> None:
    _make_package(
        pkg_root,
        "capplan_unseeded",
        assignments=(
            "v: 1\ntables:\n  demographic:\n"
            "    - steps:\n        - {op: random_sample, fraction: 0.5}\n"
        ),
    )
    planned = planned_algorithms("capplan_unseeded", ["demographic"])
    assert ("transform", "random_sample") in {(a.kind, a.name) for a in planned}
    for engine in _ENGINES:
        check_pipeline(engine, "capplan_unseeded", ["demographic"])


def test_with_replacement_refused_on_duckdb_only(pkg_root: Path) -> None:
    _make_package(
        pkg_root,
        "capplan_repl",
        mapper=(
            "v: 1\nmapper: demographic\nops:\n"
            "  - op: apply_transforms\n"
            "    steps:\n      - {op: random_sample, fraction: 0.5, with_replacement: true}\n"
        ),
    )
    with pytest.raises(CapabilityPlanError, match="sample_with_replacement"):
        check_pipeline("duckdb", "capplan_repl", ["demographic"])
    for engine in ("pyspark", "snowpark"):
        check_pipeline(engine, "capplan_repl", ["demographic"])


def test_plan_check_fires_before_runtime_unsupported() -> None:
    """The plan check catches what the engine would raise mid-run.

    DuckDB raises ``EngineOperationUnsupported`` for a with-replacement
    sample at execution time; the plan check refuses the same step first.
    """
    pytest.importorskip("duckdb")
    from ovid2.core.engines.duckdb import DuckDBEngine
    from ovid2.core.engines.protocols import EngineOperationUnsupported

    step = PlannedAlgorithm(
        "transform", "random_sample", {"fraction": 0.5, "with_replacement": True}
    )
    assert missing_capabilities("duckdb", [step]) == {
        "sample_with_replacement": ["transform:random_sample"]
    }
    engine = DuckDBEngine(args={"threads": 1, "memory_limit": "256MB"})
    try:
        frame = engine.create_typed_dataframe([("a",), ("b",)], [("x", "string")])
        with pytest.raises(EngineOperationUnsupported):
            frame.sample(withReplacement=True, fraction=0.5)
    finally:
        engine.stop_session()


def test_unknown_engine_type_is_not_checked() -> None:
    step = PlannedAlgorithm("transform", "random_sample", {"seed": 1})
    assert missing_capabilities("no_such_engine", [step]) == {}
    assert check_pipeline(None, "example_pcornet", ["demographic"])


def test_planner_reads_fixer_chains_and_adapters() -> None:
    algorithms = planned_algorithms("example_pcornet", ["diagnosis"])
    assert any(a.kind == "fixer" for a in algorithms)
    assert any(a.kind == "formatter" for a in algorithms)
    assert all(a.where in ("diagnosis", "multi-table") for a in algorithms)


def test_stage_selection_limits_the_plan() -> None:
    only_format = planned_algorithms("example_pcornet", ["diagnosis"], ["format"])
    assert {a.kind for a in only_format} == {"formatter"}
