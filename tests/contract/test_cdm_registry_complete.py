"""Contract: the CDM registry is complete and self-consistent (P10).

Every CDM shipped in :mod:`ovid2.cdm` must:

- Have a unique, snake_case name.
- Expose valid ``composite_keys`` dicts (values are tuples of strings).
- Have every ``reference_table`` present in ``composite_keys`` (a table
  cannot be a reference table unless the CDM knows about it).
- Have every ``id_columns[role].reference_table`` present in
  ``composite_keys`` when non-None.

This is a lightweight structural check — it does not require Spark.
"""

from __future__ import annotations

import re

import pytest

from ovid2.cdm import registered_cdms
from ovid2.cdm.registry import get_cdm

_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")


@pytest.mark.contract
def test_every_registered_cdm_has_a_snake_case_name() -> None:
    """CDM names must be snake_case for consistency with formatter/mapper specs."""
    offenders = [name for name in registered_cdms() if not _SNAKE_CASE.match(name)]
    assert not offenders, "CDM names must be snake_case: " + ", ".join(offenders)


@pytest.mark.contract
def test_composite_keys_are_tuples_of_strings() -> None:
    """Every composite key must be a tuple of non-empty column-name strings."""
    offenders: list[str] = []
    for name in registered_cdms():
        handle = get_cdm(name)
        for table, keys in handle.composite_keys.items():
            if not isinstance(keys, tuple):
                offenders.append(f"{name}.{table}: keys must be a tuple, got {type(keys).__name__}")
                continue
            for k in keys:
                if not isinstance(k, str) or not k:
                    offenders.append(f"{name}.{table}: key {k!r} is not a non-empty string")
    assert not offenders, "Composite key violations:\n  " + "\n  ".join(offenders)


@pytest.mark.contract
def test_reference_tables_are_in_composite_keys() -> None:
    """Reference tables must be a subset of the CDM's declared tables."""
    offenders: list[str] = []
    for name in registered_cdms():
        handle = get_cdm(name)
        stray = handle.reference_tables - set(handle.composite_keys.keys())
        if stray:
            offenders.append(f"{name}: {sorted(stray)} not in composite_keys")
    assert not offenders, "Reference-table violations:\n  " + "\n  ".join(offenders)


@pytest.mark.contract
def test_id_column_reference_tables_are_valid() -> None:
    """Every ``id_columns[role].reference_table`` must exist in the CDM."""
    offenders: list[str] = []
    for name in registered_cdms():
        handle = get_cdm(name)
        for role, id_col in handle.id_columns.items():
            if id_col.reference_table is None:
                continue
            if id_col.reference_table not in handle.composite_keys:
                offenders.append(
                    f"{name}.id_columns[{role!r}].reference_table='{id_col.reference_table}' "
                    f"is not in composite_keys"
                )
    assert not offenders, "id_column reference_table violations:\n  " + "\n  ".join(offenders)
