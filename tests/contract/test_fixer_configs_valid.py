"""Contract: every pipeline fixer_configs entry is well-formed (P10).

Walks every pipeline's ``fixer_configs.py`` (both ``FIXER_CHAINS`` and
``MULTI_TABLE_FIXES``) and asserts each entry:

1. Names a fix that is registered in
   :data:`ovid2.transformations.fixers.common.registry.COMMON_FIXES`.
2. Uses only keys that are either canonical (``fix``, ``src1``, ``src2``,
   ``table``, ``version``) or a declared alias in
   :data:`ovid2.transformations.fixers.common.registry.FIXER_ALIASES`.
3. Does not name a CDM-scoped fix that is incompatible with the
   pipeline's declared CDM (see
   :data:`ovid2.transformations.fixers.common.registry.FIXER_SCOPES`).

Pipelines without a ``fixer_configs.py`` are skipped silently.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

from ovid2.cdm.registry import resolve_cdm_for_pipeline
from ovid2.transformations.fixers.common.registry import (
    COMMON_FIXES,
    FIXER_ALIASES,
    is_compatible_with_cdm,
)

_CANONICAL_KEYS: frozenset[str] = frozenset({"fix", "src1", "src2", "table", "version"})


def _pipeline_names() -> list[str]:
    """Return every pipeline package under ``ovid2_pipelines``."""
    try:
        root = importlib.import_module("ovid2_pipelines")
    except ModuleNotFoundError:
        return []
    names: list[str] = []
    for info in pkgutil.iter_modules(root.__path__, prefix="ovid2_pipelines."):
        if info.ispkg:
            names.append(info.name.rsplit(".", 1)[-1])
    return names


def _load_configs(pipeline: str) -> tuple[dict[str, list[dict[str, object]]], list[dict[str, object]]]:
    """Return ``(FIXER_CHAINS, MULTI_TABLE_FIXES)`` for a pipeline, or empties."""
    try:
        module = importlib.import_module(f"ovid2_pipelines.{pipeline}.fixer_configs")
    except ModuleNotFoundError:
        return {}, []
    chains = getattr(module, "FIXER_CHAINS", {}) or {}
    multi = list(getattr(module, "MULTI_TABLE_FIXES", []) or [])
    return chains, multi


@pytest.mark.contract
def test_every_fix_chain_names_a_registered_fixer() -> None:
    """Every ``fix:`` field in a chain must resolve to a registered fixer."""
    offenders: list[str] = []
    for pipeline in _pipeline_names():
        chains, multi = _load_configs(pipeline)
        for table, entries in chains.items():
            for i, entry in enumerate(entries):
                fix_name = str(entry.get("fix", ""))
                if fix_name and fix_name not in COMMON_FIXES:
                    offenders.append(
                        f"{pipeline}.FIXER_CHAINS['{table}'][{i}]: unknown fix '{fix_name}'"
                    )
        for i, entry in enumerate(multi):
            fix_name = str(entry.get("fix", ""))
            if fix_name and fix_name not in COMMON_FIXES:
                offenders.append(
                    f"{pipeline}.MULTI_TABLE_FIXES[{i}]: unknown fix '{fix_name}'"
                )
    assert not offenders, "Unknown fixer references:\n  " + "\n  ".join(offenders)


@pytest.mark.contract
def test_every_fix_chain_uses_recognised_keys() -> None:
    """Config entry keys must be canonical or declared aliases for their fixer."""
    offenders: list[str] = []
    for pipeline in _pipeline_names():
        chains, multi = _load_configs(pipeline)

        def _check(where: str, entry: dict[str, object]) -> None:
            fix_name = str(entry.get("fix", ""))
            allowed = set(_CANONICAL_KEYS)
            allowed.update(FIXER_ALIASES.get(fix_name, {}).keys())
            for key in entry:
                if key not in allowed:
                    offenders.append(f"{where}: unrecognised key '{key}' for fix '{fix_name}'")

        for table, entries in chains.items():
            for i, entry in enumerate(entries):
                _check(f"{pipeline}.FIXER_CHAINS['{table}'][{i}]", entry)
        for i, entry in enumerate(multi):
            _check(f"{pipeline}.MULTI_TABLE_FIXES[{i}]", entry)

    assert not offenders, "Unrecognised fixer-config keys:\n  " + "\n  ".join(offenders)


@pytest.mark.contract
def test_no_pipeline_uses_cdm_incompatible_fixers() -> None:
    """A pipeline on CDM X may not name a fixer scoped to a different CDM."""
    offenders: list[str] = []
    for pipeline in _pipeline_names():
        try:
            cdm = resolve_cdm_for_pipeline(pipeline)
        except ValueError:
            # Pipeline has no settings — nothing to enforce.
            continue

        chains, multi = _load_configs(pipeline)
        for table, entries in chains.items():
            for i, entry in enumerate(entries):
                fix_name = str(entry.get("fix", ""))
                if not fix_name:
                    continue
                if not is_compatible_with_cdm(fix_name, cdm.name):
                    offenders.append(
                        f"{pipeline}.FIXER_CHAINS['{table}'][{i}]: fix '{fix_name}' is scoped to "
                        f"a different CDM than '{cdm.name}'"
                    )
        for i, entry in enumerate(multi):
            fix_name = str(entry.get("fix", ""))
            if not fix_name:
                continue
            if not is_compatible_with_cdm(fix_name, cdm.name):
                offenders.append(
                    f"{pipeline}.MULTI_TABLE_FIXES[{i}]: fix '{fix_name}' is scoped to "
                    f"a different CDM than '{cdm.name}'"
                )
    assert not offenders, "CDM-scope violations:\n  " + "\n  ".join(offenders)
