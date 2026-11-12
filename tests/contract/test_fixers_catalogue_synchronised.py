"""Contract: the web-safe fixer catalogue mirrors the fixer registry."""

from __future__ import annotations

import inspect

import pytest

from ovid2.config.fixers_catalogue import FIXER_CATALOGUE
from ovid2.transformations.fixers.common.registry import (
    COMMON_FIXES,
    FIXER_ALIASES,
    scope_of,
)


@pytest.mark.contract
def test_fixer_catalogue_matches_registry() -> None:
    assert set(FIXER_CATALOGUE) == set(COMMON_FIXES), (
        "config/fixers_catalogue.py is out of sync with COMMON_FIXES"
    )
    for name, cls in COMMON_FIXES.items():
        entry = FIXER_CATALOGUE[name]
        assert entry["scope"] == scope_of(name), name
        assert entry["aliases"] == FIXER_ALIASES.get(name, {}), name
        assert entry["summary"] == (inspect.getdoc(cls) or "").splitlines()[0], name
