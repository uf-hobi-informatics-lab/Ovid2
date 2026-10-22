"""Contract: every registered connection type maps to an ODS dimension.

Ensures :mod:`ovid2.dataspace.registry` never drifts from
:data:`ovid2.connections.registry.CONNECTION_TYPE_MAP`. Adding a new
connector must not silently create an unnamed dimension.
"""

from __future__ import annotations

from ovid2.connections.registry import CONNECTION_TYPE_MAP
from ovid2.dataspace.registry import list_dimensions


def test_dimensions_match_connection_types() -> None:
    assert sorted(list_dimensions()) == sorted(CONNECTION_TYPE_MAP.keys())


def test_all_dimensions_are_strings() -> None:
    for dim in list_dimensions():
        assert isinstance(dim, str)
        assert dim  # non-empty
