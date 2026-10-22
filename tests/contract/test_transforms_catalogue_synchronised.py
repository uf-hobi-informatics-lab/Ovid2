"""Contract: the web-safe transforms catalogue mirrors the registry.

The web/MCP tiers enumerate transforms through
:mod:`ovid2.config.transforms_catalogue` (web-safe, no pipeline import),
while the pipeline tier runs them through
:data:`ovid2.transforms.registry.TRANSFORMS`. This test proves the two
never drift: every registered transform's discovery metadata (name,
summary, category, requires_family, params_schema) must appear verbatim
in the catalogue, and the catalogue must not carry phantom entries.

This is the transforms analogue of the engine registry/config split
guard (``tests/unit/core/engines/test_registry.py``).
"""

from __future__ import annotations

import pytest


def _registry_metadata() -> dict[str, dict[str, object]]:
    """Return ``{name: metadata}`` derived from the live registry."""
    from ovid2.transforms.registry import TRANSFORMS

    out: dict[str, dict[str, object]] = {}
    for name, cls in TRANSFORMS.items():
        inst = cls()
        out[name] = {
            "name": inst.name,
            "summary": inst.summary,
            "category": inst.category,
            "requires_family": inst.requires_family,
            "params_schema": inst.params_schema,
        }
    return out


def _catalogue_metadata() -> dict[str, dict[str, object]]:
    """Return ``{name: metadata}`` from the web-safe catalogue."""
    from ovid2.config.transforms_catalogue import TRANSFORM_CATALOGUE

    return {str(entry["name"]): dict(entry) for entry in TRANSFORM_CATALOGUE}


@pytest.mark.contract
def test_catalogue_matches_registry() -> None:
    """Catalogue and registry agree on every field of every transform."""
    registry = _registry_metadata()
    catalogue = _catalogue_metadata()

    assert set(registry) == set(catalogue), (
        "Catalogue / registry name mismatch.\n"
        f"  only in registry:  {sorted(set(registry) - set(catalogue))}\n"
        f"  only in catalogue: {sorted(set(catalogue) - set(registry))}\n"
        "Regenerate src/ovid2/config/transforms_catalogue.py from the registry."
    )

    for name in sorted(registry):
        assert catalogue[name] == registry[name], (
            f"Catalogue entry for '{name}' differs from the registry metadata.\n"
            f"  registry:  {registry[name]}\n"
            f"  catalogue: {catalogue[name]}"
        )


@pytest.mark.contract
def test_catalogue_categories_cover_entries() -> None:
    """Every catalogue entry's category is in the published CATEGORIES."""
    from ovid2.config.transforms_catalogue import CATEGORIES, TRANSFORM_CATALOGUE

    for entry in TRANSFORM_CATALOGUE:
        assert entry["category"] in CATEGORIES, (
            f"Transform '{entry['name']}' category '{entry['category']}' "
            f"not in CATEGORIES {CATEGORIES}."
        )
