"""Web-tier view over the neutral run-kind registry.

The single source of truth for run kinds is :mod:`ovid2.run_kinds` (a
top-level module importable by both the executor and the web tier). This
module re-exports it for web callers and adds a small serialisation
helper (:func:`kind_choices`) that the New-pipeline / Submit templates
consume to render the type ``<select>`` and its per-option descriptions
without hardcoding the list.

Adding a new run kind is a single :class:`~ovid2.run_kinds.KindSpec`
entry in :mod:`ovid2.run_kinds`; the dropdown, the Pipelines page
grouping, create/submit validation, and the worker argv all follow
automatically.
"""

from __future__ import annotations

from typing import Any

from ovid2.run_kinds import (
    KIND_ALIASES,
    KIND_REGISTRY,
    KindSpec,
    all_kinds,
    get_kind,
    kind_for_command,
    normalize_kind,
    package_free_kinds,
)

__all__ = [
    "KIND_ALIASES",
    "KIND_REGISTRY",
    "KindSpec",
    "all_kinds",
    "get_kind",
    "kind_choices",
    "kind_flags",
    "kind_for_command",
    "normalize_kind",
    "package_free_kinds",
]


def kind_choices() -> list[dict[str, Any]]:
    """Return ``[{name, label, description, flags…}, ...]`` in display order.

    Used by the New-pipeline / Submit / Edit templates to render the
    run-kind picker and — UI rule U1 — to decide which fields are
    relevant. Each entry carries the :class:`~ovid2.run_kinds.KindSpec`
    flags the browser needs (``uses_cdm``, ``uses_transformations``,
    ``tables_mode``, ``allows_extra_mounts``, ``allows_strict_types``,
    ``health_selection``, ``create_flow``, ``uses_engine_overrides``) so
    pages never hard-code kind names (``static/field_visibility.js``).
    """
    return [kind_flags(spec) for spec in KIND_REGISTRY.values() if not spec.job_only]


def kind_flags(spec: KindSpec) -> dict[str, Any]:
    """Return the browser-facing description of one run kind.

    ``uses_engine_overrides`` is ``True`` for kinds whose worker argv
    forwards ``--workers`` / ``--sf_account`` / ``--stderr-log`` (the
    ``etl`` and ``health`` argv builders); copy / move drop them.
    ``is_transfer`` marks the address-to-address kinds (copy / move) that
    take a free job name instead of a pipeline package.

    Args:
        spec: The kind's registry entry.

    Returns:
        A JSON-serialisable dict.
    """
    return {
        "name": spec.name,
        "label": spec.label,
        "description": spec.description,
        "uses_cdm": spec.uses_cdm,
        "uses_transformations": spec.uses_transformations,
        "tables_mode": spec.tables_mode,
        "allows_extra_mounts": spec.allows_extra_mounts,
        "allows_strict_types": spec.allows_strict_types,
        "health_selection": spec.health_selection,
        "create_flow": spec.create_flow,
        "uses_engine_overrides": spec.argv_builder in ("etl", "health"),
        "is_transfer": spec.argv_builder in ("copy", "move"),
        "package_required": spec.package_required,
    }
