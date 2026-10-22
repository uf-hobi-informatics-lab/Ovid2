"""Contract: the public transformations registry is well-formed.

Every entry in :data:`ovid2.transforms.registry.TRANSFORMS` must:

* be a concrete :class:`~ovid2.transforms.base.BaseTransform` subclass,
* expose a non-empty ``name`` that matches its registry key,
* expose a non-empty ``summary``,
* declare a ``category`` from the known set,
* declare ``requires_family`` as ``None`` or a known family,
* expose a ``params_schema`` that is a JSON-schema object.
"""

from __future__ import annotations

import pytest

_KNOWN_CATEGORIES = frozenset({"formatting", "correction", "reshape", "sampling"})
_KNOWN_FAMILIES = frozenset({"spark", "snowpark"})


@pytest.mark.contract
def test_registry_entries_well_formed() -> None:
    """Every registered transform satisfies the metadata contract."""
    from ovid2.transforms.base import BaseTransform
    from ovid2.transforms.registry import TRANSFORMS

    assert TRANSFORMS, "TRANSFORMS registry is empty."

    for key, cls in TRANSFORMS.items():
        assert isinstance(cls, type) and issubclass(cls, BaseTransform), (
            f"Registry entry '{key}' is not a BaseTransform subclass."
        )
        inst = cls()
        assert inst.name == key, (
            f"Transform '{key}' reports name '{inst.name}'; key and name must match."
        )
        assert inst.summary and isinstance(inst.summary, str), (
            f"Transform '{key}' must expose a non-empty summary."
        )
        assert inst.category in _KNOWN_CATEGORIES, (
            f"Transform '{key}' has unknown category '{inst.category}'."
        )
        assert inst.requires_family is None or inst.requires_family in _KNOWN_FAMILIES, (
            f"Transform '{key}' has unknown requires_family '{inst.requires_family}'."
        )
        schema = inst.params_schema
        assert isinstance(schema, dict) and schema.get("type") == "object", (
            f"Transform '{key}' params_schema must be a JSON-schema object."
        )


@pytest.mark.contract
def test_transform_names_sorted_and_unique() -> None:
    """``transform_names()`` returns a sorted, de-duplicated list."""
    from ovid2.transforms.registry import transform_names

    names = transform_names()
    assert names == sorted(names)
    assert len(names) == len(set(names))
