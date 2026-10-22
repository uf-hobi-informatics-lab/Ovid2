"""Pipeline run-plan service — read-only.

Answers "what would this pipeline run?" for the submit-page table /
transformation pickers:

- **Tables** — the pipeline's CDM table roster (via
  :func:`ovid2.cdm.registry.resolve_cdm_for_pipeline`) filtered by a
  formatter-presence probe. This reproduces the ``--tables all``
  expansion :func:`ovid2.core.loaders.resolve_tables` performs at run
  time, but *without* importing the pipeline tier — the web tier is
  forbidden from importing ``ovid2.core`` /
  ``ovid2.formatters`` / ``ovid2.mappers`` /
  ``ovid2.transformations`` (see
  ``tests/contract/test_web_no_pipeline_imports.py``). Importing
  ``ovid2.cdm`` is allowed and already done by ``cdms_svc``.

- **Transformations** — the four canonical ETL stages. This list is a
  fixed constant (``format``, ``deduplicate``, ``map``, ``fix``);
  duplicating it here is not a pipeline import.

- **Fixers** — the per-table fix chains declared in the pipeline's
  ``fixer_configs.py`` (``FIXER_CHAINS`` + ``MULTI_TABLE_FIXES``),
  surfaced read-only so the operator can see what the ``fix`` stage
  will do. The pipeline package is *not* a forbidden root, so importing
  ``ovid2_pipelines.<pipeline>.fixer_configs`` is fine.

The formatter-presence probe mirrors
:func:`ovid2.core.loaders.has_formatter` exactly (three probes:
importable ``formatters.<table>`` module, on-disk
``formatters/<table>.yaml``, importable
``formatters_custom.<table>_formatter`` module) so the submit-page
picker agrees with the pipeline.
"""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from typing import Any

from ovid2.web.models import (
    FixerPlanEntry,
    PipelinePlanResponse,
    TablePlanEntry,
)

#: Canonical ETL transformation stages, in run order. Mirrors
#: :func:`ovid2.core.loaders.get_all_transformations` — a fixed constant,
#: intentionally duplicated so the web tier need not import the pipeline.
TRANSFORMATION_STAGES: tuple[str, ...] = ("format", "deduplicate", "map", "fix")

__all__ = [
    "TRANSFORMATION_STAGES",
    "PipelinePlanError",
    "get_pipeline_plan",
    "list_pipeline_fixers",
    "list_pipeline_tables",
]


class PipelinePlanError(ValueError):
    """Raised when a pipeline's plan cannot be resolved."""


def _module_exists(dotted_name: str) -> bool:
    """Return True iff ``dotted_name`` can be imported without executing it.

    Mirrors :func:`ovid2.core.loaders._module_exists`.
    """
    try:
        return importlib.util.find_spec(dotted_name) is not None
    except ModuleNotFoundError:
        return False


def _pipeline_pkg_paths(pipeline: str) -> list[Path]:
    """Return the on-disk search dirs of a pipeline package (may be empty)."""
    try:
        pkg = importlib.import_module(f"ovid2_pipelines.{pipeline}")
    except ModuleNotFoundError:
        return []
    return [Path(p) for p in getattr(pkg, "__path__", [])]


def _formatter_source(pipeline: str, table: str) -> str:
    """Return which formatter probe matched for ``(pipeline, table)``.

    Returns ``'python'`` | ``'yaml'`` | ``'custom'`` | ``''`` (none).
    Probe order matches :func:`ovid2.core.loaders.has_formatter`.
    """
    if _module_exists(f"ovid2_pipelines.{pipeline}.formatters.{table}"):
        return "python"
    for base in _pipeline_pkg_paths(pipeline):
        if (base / "formatters" / f"{table}.yaml").is_file():
            return "yaml"
    if _module_exists(f"ovid2_pipelines.{pipeline}.formatters_custom.{table}_formatter"):
        return "custom"
    return ""


def _resolve_cdm_tables(pipeline: str) -> tuple[str, list[str]]:
    """Return ``(cdm_name, tables)`` for ``pipeline``.

    Raises:
        PipelinePlanError: When the pipeline's CDM cannot be resolved.
    """
    from ovid2.cdm.registry import resolve_cdm_for_pipeline

    try:
        handle = resolve_cdm_for_pipeline(pipeline)
    except ValueError as exc:
        raise PipelinePlanError(str(exc)) from None
    return handle.name, list(handle.tables())


def list_pipeline_tables(pipeline: str) -> tuple[str, list[TablePlanEntry]]:
    """Return ``(cdm_name, entries)`` — the pipeline's CDM tables + formatter flag.

    Each entry carries ``has_formatter`` (True when ``--tables all`` would
    include it) and the matching ``source`` probe. The order matches the
    CDM's insertion-ordered table roster.
    """
    cdm_name, tables = _resolve_cdm_tables(pipeline)
    entries: list[TablePlanEntry] = []
    for table in tables:
        source = _formatter_source(pipeline, table)
        entries.append(
            TablePlanEntry(
                name=table,
                has_formatter=bool(source),
                source=source,
            )
        )
    return cdm_name, entries


def _fixer_detail(config: dict[str, Any]) -> str:
    """Return a compact, human-facing summary of one fix config's args."""
    parts: list[str] = []
    for key, value in config.items():
        if key == "fix":
            continue
        parts.append(f"{key}={value}")
    return ", ".join(parts)


def _coerce_fixer_entry(config: Any) -> FixerPlanEntry | None:
    """Coerce one raw fix config into a :class:`FixerPlanEntry`, or None."""
    if not isinstance(config, dict):
        return None
    fix_name = config.get("fix")
    if not fix_name:
        return None
    return FixerPlanEntry(fix=str(fix_name), detail=_fixer_detail(config))


def list_pipeline_fixers(
    pipeline: str,
) -> tuple[dict[str, list[FixerPlanEntry]], list[FixerPlanEntry]]:
    """Return ``(per_table_chains, multi_table)`` from the pipeline's config.

    Reads ``ovid2_pipelines.<pipeline>.fixer_configs``. A pipeline with no
    such module (or no chains) yields empty structures — never raises.
    """
    try:
        mod = importlib.import_module(f"ovid2_pipelines.{pipeline}.fixer_configs")
    except ModuleNotFoundError:
        return {}, []

    chains_raw = getattr(mod, "FIXER_CHAINS", {}) or {}
    per_table: dict[str, list[FixerPlanEntry]] = {}
    if isinstance(chains_raw, dict):
        for table, configs in chains_raw.items():
            if not isinstance(configs, (list, tuple)):
                continue
            entries = [e for e in (_coerce_fixer_entry(c) for c in configs) if e is not None]
            if entries:
                per_table[str(table)] = entries

    multi_raw = getattr(mod, "MULTI_TABLE_FIXES", []) or []
    multi: list[FixerPlanEntry] = []
    if isinstance(multi_raw, (list, tuple)):
        multi = [e for e in (_coerce_fixer_entry(c) for c in multi_raw) if e is not None]
    return per_table, multi


def get_pipeline_plan(pipeline: str) -> PipelinePlanResponse:
    """Return the full run plan for ``pipeline``.

    Raises:
        PipelinePlanError: When the pipeline package or its CDM is missing.
    """
    if not pipeline:
        raise PipelinePlanError("Pipeline name is required.")
    # A pipeline with no package at all should 404 rather than return an
    # empty (but valid-looking) plan.
    if not _pipeline_pkg_paths(pipeline) and not _module_exists(f"ovid2_pipelines.{pipeline}"):
        raise PipelinePlanError(f"Pipeline '{pipeline}' not found.")

    cdm_name, tables = list_pipeline_tables(pipeline)
    per_table, multi = list_pipeline_fixers(pipeline)
    return PipelinePlanResponse(
        pipeline=pipeline,
        cdm=cdm_name,
        tables=tables,
        transformations=list(TRANSFORMATION_STAGES),
        fixers=per_table,
        multi_table_fixers=multi,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("This module is imported by the ovid2 web routes; it is not a script.")
