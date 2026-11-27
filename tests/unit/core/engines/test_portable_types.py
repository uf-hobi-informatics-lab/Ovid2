"""Unit tests for the portable ``Row`` in :mod:`ovid2.core.engines.types`."""

from __future__ import annotations

import pytest

from ovid2.core.engines.types import Row


@pytest.mark.unit
def test_row_field_lookup_is_exact_first() -> None:
    row = Row(a=1, A=2)
    assert row["a"] == 1
    assert row["A"] == 2


@pytest.mark.unit
def test_row_falls_back_to_case_insensitive_lookup() -> None:
    """Snowpark rows carry folded names (``MAX_DATE``); Spark code asks for ``max_date``."""
    row = Row(MAX_DATE="2026-01-01")
    assert row["max_date"] == "2026-01-01"
    assert row.max_date == "2026-01-01"


@pytest.mark.unit
def test_row_ambiguous_or_missing_name_raises() -> None:
    row = Row(x=1, X=2)
    with pytest.raises(ValueError):
        row["Y"]
    with pytest.raises(AttributeError):
        _ = row.missing
