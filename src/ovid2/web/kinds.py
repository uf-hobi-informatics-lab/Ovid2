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
    "kind_for_command",
    "normalize_kind",
    "package_free_kinds",
]


def kind_choices() -> list[dict[str, str]]:
    """Return ``[{name, label, description}, ...]`` in display order.

    Used by the New-pipeline / Submit templates to render the run-kind
    ``<select>`` with human labels and per-option help text, driven
    entirely by :data:`~ovid2.run_kinds.KIND_REGISTRY`.
    """
    return [
        {"name": spec.name, "label": spec.label, "description": spec.description}
        for spec in KIND_REGISTRY.values()
    ]
