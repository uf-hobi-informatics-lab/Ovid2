"""Plan-time capability check: refuse a run its engine cannot execute.

Before any cluster starts (``ovid2 etl`` main, next to
:mod:`ovid2.drivers.engine_compat`) and before a job is queued
(``jobs_svc.submit``), :func:`check_pipeline` collects the algorithms a run
will select **statically** — YAML / ``ast`` only, the same rules as the
pipeline inspector — and compares the union of their feature requirements
(:mod:`ovid2.config.algorithm_requirements`) with the engine's declared
capabilities (:mod:`ovid2.config.capabilities`). A gap raises
:class:`CapabilityPlanError` (``ovid2 etl`` exits ``2``; the web tier answers
HTTP 400). With the check in place, a runtime
:class:`~ovid2.core.engines.protocols.EngineOperationUnsupported` is a bug.

What is collected, per selected table and stage:

* ``format`` — the formatter spec's ``adapter`` (YAML specs);
* ``deduplicate`` — the composite-key deduplicator;
* ``transform`` — every op of every assigned chain (``assignments.yaml``;
  named chains resolved like ``transforms.spec.find_spec``);
* ``map`` — every ``op`` in the pipeline's (or the CDM default) mapper YAML,
  plus the ops of ``apply_transforms`` inline / named chains;
* ``fix`` — every ``fix`` in the table's fixer chain, and the multi-table
  fixes.

Web-safe: imports only ``ovid2.config.*`` and ``yaml`` (the web tier calls
it; ``test_tier_import_closure.py``).
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ovid2.config.algorithm_requirements import features_for
from ovid2.config.capabilities import Capability, engine_capabilities

__all__ = [
    "CapabilityPlanError",
    "PlannedAlgorithm",
    "check_pipeline",
    "missing_capabilities",
    "planned_algorithms",
]


class CapabilityPlanError(RuntimeError):
    """The engine lacks capabilities the run's algorithms require.

    Attributes:
        engine_type: The engine type checked.
        missing: ``{capability: [kind:name, ...]}``.
    """

    def __init__(self, engine_type: str, missing: dict[str, list[str]]) -> None:
        detail = "; ".join(
            f"'{cap}' (needed by {', '.join(sorted(users))})"
            for cap, users in sorted(missing.items())
        )
        super().__init__(
            f"Engine '{engine_type}' cannot run this pipeline: it lacks {detail}. "
            "Pick an engine that supports these capabilities (docs/engines.md → "
            "Capabilities) or change the step's parameters."
        )
        self.engine_type = engine_type
        self.missing = missing


@dataclass(frozen=True)
class PlannedAlgorithm:
    """One algorithm a run will select."""

    kind: str
    name: str
    params: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)
    where: str = ""

    @property
    def label(self) -> str:
        """``kind:name`` (plus where it was found)."""
        base = f"{self.kind}:{self.name}"
        return f"{base} [{self.where}]" if self.where else base


# --------------------------------------------------------------------------- #
# Static discovery                                                             #
# --------------------------------------------------------------------------- #


def _package_dir(pipeline: str) -> Path | None:
    spec = importlib.util.find_spec(f"ovid2_pipelines.{pipeline}")
    if spec is None or not spec.submodule_search_locations:
        return None
    for base in spec.submodule_search_locations:
        if Path(base).is_dir():
            return Path(base)
    return None


def _yaml(path: Path) -> dict[str, Any]:
    import yaml

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _first_yaml(directory: Path, stem: str) -> Path | None:
    for suffix in (".yaml", ".yml"):
        candidate = directory / f"{stem}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def _shipped_root() -> Path:
    import ovid2

    return Path(ovid2.__file__).resolve().parent / "resources"


def _chain_steps(name: str, pkg: Path | None) -> list[dict[str, Any]]:
    """Return a named transform chain's steps (pipeline → user → shipped)."""
    from ovid2.config.transform_assignments import user_library_dir

    dirs = [d for d in ((pkg / "transforms") if pkg else None, user_library_dir()) if d]
    dirs.append(_shipped_root() / "transforms")
    for directory in dirs:
        path = _first_yaml(directory, name)
        if path is not None:
            steps = _yaml(path).get("steps") or []
            return [s for s in steps if isinstance(s, dict)]
    return []


def _transform_steps(
    steps: Iterable[dict[str, Any]], where: str, pkg: Path | None
) -> list[PlannedAlgorithm]:
    out: list[PlannedAlgorithm] = []
    for step in steps:
        op = str(step.get("op") or "")
        if op:
            params = {k: v for k, v in step.items() if k != "op"}
            out.append(PlannedAlgorithm("transform", op, params, where))
    del pkg
    return out


def _settings_constant(pkg: Path, name: str) -> str:
    import ast

    settings = pkg / "pipeline_settings.py"
    if not settings.is_file():
        return ""
    try:
        tree = ast.parse(settings.read_text(encoding="utf-8"))
    except SyntaxError:
        return ""
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
            and isinstance(node.value, ast.Constant)
        ):
            return str(node.value.value)
    return ""


def _cdm_of(pipeline: str) -> str:
    try:
        from ovid2.cdm.registry import resolve_cdm_for_pipeline

        return str(resolve_cdm_for_pipeline(pipeline).name)
    except (ValueError, KeyError, ModuleNotFoundError):
        return ""


_ALL_STAGES: frozenset[str] = frozenset({"format", "deduplicate", "transform", "map", "fix"})


def _cdm_tables(pipeline: str) -> list[str]:
    try:
        from ovid2.cdm.registry import resolve_cdm_for_pipeline

        return list(resolve_cdm_for_pipeline(pipeline).tables())
    except (ValueError, KeyError, ModuleNotFoundError):
        return []


def planned_algorithms(
    pipeline: str,
    tables: Iterable[str],
    stages: Iterable[str] = ("format", "deduplicate", "transform", "map", "fix"),
) -> list[PlannedAlgorithm]:
    """Return every algorithm a run of ``pipeline`` over ``tables`` selects.

    Static (YAML / ``ast``): never imports pipeline or algorithm code.
    Unresolvable pieces are skipped — the run will report them itself.
    """
    pkg = _package_dir(pipeline)
    stage_set = set(stages) if "all" not in set(stages) else _ALL_STAGES
    table_list = [str(t).split("=", 1)[0] for t in tables if t]
    if table_list == ["all"] or not table_list:
        table_list = _cdm_tables(pipeline)
    out: list[PlannedAlgorithm] = []
    if pkg is None:
        return out
    # Mirrors ``MapTransformer``: the CDM default mapper spec is only used
    # under the ``legacy_ovid`` profile.
    legacy = _settings_constant(pkg, "ETL_PROFILE") == "legacy_ovid"
    cdm = _cdm_of(pipeline) if legacy else ""

    if "transform" in stage_set:
        try:
            from ovid2.config.transform_assignments import load_assignments, table_assignments

            assignments = load_assignments(pipeline)
        except Exception:  # an invalid file fails the run itself
            assignments = {}
    else:
        assignments = {}

    chains: dict[str, list[dict[str, Any]]] = {}
    multi: list[dict[str, Any]] = []
    if "fix" in stage_set:
        try:
            from ovid2.config.fixer_configs import load_fixer_configs

            chains, multi, _source = load_fixer_configs(pipeline)
        except Exception:
            chains, multi = {}, []

    for table in table_list:
        if "format" in stage_set:
            path = _first_yaml(pkg / "formatters", table)
            if path is not None:
                adapter = str(_yaml(path).get("adapter") or "")
                if adapter:
                    out.append(PlannedAlgorithm("formatter", adapter, where=table))
        if "deduplicate" in stage_set:
            out.append(PlannedAlgorithm("deduplicator", "composite_key", where=table))
        if assignments:
            for assignment in table_assignments(assignments, table):
                steps = (
                    _chain_steps(assignment.spec, pkg)
                    if assignment.spec
                    else list(assignment.steps or ())
                )
                out.extend(_transform_steps(steps, table, pkg))
        if "map" in stage_set:
            path = _first_yaml(pkg / "mappers", table)
            if path is None and cdm:
                path = _first_yaml(_shipped_root() / "cdms" / cdm / "mappers", table)
            for op in (_yaml(path).get("ops") if path else None) or []:
                if not isinstance(op, dict) or not op.get("op"):
                    continue
                name = str(op["op"])
                out.append(PlannedAlgorithm("mapper", name, where=table))
                if name == "apply_transforms":
                    params = op.get("params") or op
                    if params.get("spec"):
                        steps = _chain_steps(str(params["spec"]), pkg)
                    else:
                        steps = [s for s in params.get("steps") or [] if isinstance(s, dict)]
                    out.extend(_transform_steps(steps, table, pkg))
        for entry in chains.get(table, []) if "fix" in stage_set else []:
            if entry.get("fix"):
                out.append(PlannedAlgorithm("fixer", str(entry["fix"]), where=table))
    for entry in multi:
        if entry.get("fix"):
            out.append(PlannedAlgorithm("fixer", str(entry["fix"]), where="multi-table"))
    return out


# --------------------------------------------------------------------------- #
# Check                                                                        #
# --------------------------------------------------------------------------- #


def missing_capabilities(
    engine_type: str, algorithms: Iterable[PlannedAlgorithm]
) -> dict[str, list[str]]:
    """Return ``{capability: [users]}`` the engine lacks (empty = runnable).

    An unknown engine type is not checked (``{}``) — the engine loader
    reports it.
    """
    declared = engine_capabilities(engine_type)
    if declared is None:
        return {}
    have = {c.value for c in declared}
    missing: dict[str, set[str]] = {}
    for algorithm in algorithms:
        for feature in features_for(algorithm.kind, algorithm.name, algorithm.params):
            if feature not in have:
                missing.setdefault(feature, set()).add(f"{algorithm.kind}:{algorithm.name}")
    return {cap: sorted(users) for cap, users in missing.items()}


def check_pipeline(
    engine_type: str | None,
    pipeline: str,
    tables: Iterable[str],
    stages: Iterable[str] = ("format", "deduplicate", "transform", "map", "fix"),
) -> list[PlannedAlgorithm]:
    """Refuse the plan when ``engine_type`` lacks a required capability.

    Returns:
        The planned algorithms (for logging).

    Raises:
        CapabilityPlanError: When the engine lacks a capability.
    """
    algorithms = planned_algorithms(pipeline, tables, stages)
    if not engine_type:
        return algorithms
    missing = missing_capabilities(engine_type, algorithms)
    if missing:
        raise CapabilityPlanError(engine_type, missing)
    return algorithms


def capability_names(values: Iterable[Capability]) -> list[str]:
    """Return sorted capability values (for messages / JSON)."""
    return sorted(c.value for c in values)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.config.capability_plan is a library module.")
