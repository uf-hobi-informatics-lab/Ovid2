"""Contract: the web-safe mapper-op catalogue covers exactly the registry."""

from __future__ import annotations

import pytest

from ovid2.algorithms.registry import registry
from ovid2.config.mapper_ops_catalogue import MAPPER_OP_CATALOGUE

MAP_OPS = registry.native_entries("mapper")


@pytest.mark.contract
def test_mapper_op_catalogue_matches_registry() -> None:
    assert set(MAPPER_OP_CATALOGUE) == set(MAP_OPS), (
        "config/mapper_ops_catalogue.py is out of sync with the AlgorithmRegistry mapper ops"
    )
    for name, entry in MAPPER_OP_CATALOGUE.items():
        assert entry["summary"], name
        assert entry["params_schema"].get("type") == "object", name
