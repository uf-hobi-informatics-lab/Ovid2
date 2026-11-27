"""Contract: the web-safe fixer catalogue mirrors the fixer registry."""

from __future__ import annotations

import inspect

import pytest

from ovid2.algorithms.registry import registry
from ovid2.config.fixers_catalogue import FIXER_CATALOGUE


@pytest.mark.contract
def test_fixer_catalogue_matches_registry() -> None:
    fixers = registry.native_entries("fixer")
    assert set(FIXER_CATALOGUE) == set(fixers), (
        "config/fixers_catalogue.py is out of sync with the AlgorithmRegistry fixers"
    )
    for name, cls in fixers.items():
        entry = FIXER_CATALOGUE[name]
        info = registry.describe("fixer", name)
        assert entry["scope"] == info["scope"], name
        assert entry["aliases"] == info["aliases"], name
        assert entry["summary"] == (inspect.getdoc(cls) or "").splitlines()[0], name
