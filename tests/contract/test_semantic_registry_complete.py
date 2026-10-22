"""Contract: the semantic registry is complete and self-consistent (S1).

Every semantic handle bundled with Ovid2 must:

- Have a valid ``kind`` and snake_case ``name``.
- Pass its own ``__post_init__`` invariants (this test doubles as a
  smoke test that no bundled handle raises on import).
- Have every :class:`~ovid2.semantic.registry.ColumnBinding` reference
  a registered vocabulary and an existing ``(cdm, table, column)``.

This is a lightweight structural check — it does not require Spark.
"""

from __future__ import annotations

import re

import pytest

import ovid2.semantic as semantic
from ovid2.cdm import registered_cdms
from ovid2.cdm.registry import get_cdm

_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")
#: Cross-CDM vocabularies use plain snake_case (`omop_concept`);
#: CDM-scoped valuesets use dotted-lowercase (`pcornet_7_0.demographic.sex`).
#: Both are legal after phase C1.
_DOTTED_LOWERCASE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]+)*$")


@pytest.mark.contract
def test_every_semantic_handle_has_a_snake_case_name() -> None:
    """Handle names must be snake_case or dotted-lowercase (C1)."""
    offenders: list[str] = []
    for kind in semantic.registered_kinds():
        for name in semantic.registered(kind):
            if not _DOTTED_LOWERCASE.match(name):
                offenders.append(f"{kind}:{name}")
    assert not offenders, (
        "Semantic handle names must be snake_case or dotted-lowercase: "
        + ", ".join(offenders)
    )


@pytest.mark.contract
def test_every_vocabulary_declares_a_code_column() -> None:
    """Every ``vocabulary`` handle must set ``code_column``."""
    offenders: list[str] = []
    for name in semantic.registered("vocabulary"):
        handle = semantic.get_vocabulary(name)
        if not handle.code_column:
            offenders.append(name)
    assert not offenders, "Vocabularies missing code_column: " + ", ".join(offenders)


@pytest.mark.contract
def test_every_vocabulary_has_a_resolvable_source() -> None:
    """Every ``vocabulary`` handle must resolve to exactly one source leg.

    The four legs (phase C1) are ``source_path``, ``shared_view``,
    ``address``, and ``inline_items``. ``__post_init__`` already enforces
    exclusivity — this test guards against a handle being constructed
    with none set (which the dataclass allows in principle).
    """
    offenders: list[str] = []
    for name in semantic.registered("vocabulary"):
        handle = semantic.get_vocabulary(name)
        try:
            leg = handle.source_leg()
        except AttributeError:
            offenders.append(f"{name}: pre-C1 handle without source_leg()")
            continue
        if leg not in {"source_path", "shared_view", "address", "inline_items"}:
            offenders.append(f"{name}: unknown source leg {leg!r}")
            continue
        if not handle.resolved_view_name():
            offenders.append(f"{name} (empty view_name)")
    assert not offenders, "Vocabularies with unresolvable source:\n  " + "\n  ".join(offenders)


@pytest.mark.contract
def test_every_binding_references_a_registered_vocabulary() -> None:
    """Every :class:`ColumnBinding` must point at a registered vocabulary."""
    known = set(semantic.registered("vocabulary"))
    offenders = [
        f"{b.cdm}.{b.table}.{b.column} -> {b.vocabulary}"
        for b in semantic.all_bindings()
        if b.vocabulary not in known
    ]
    assert not offenders, "Bindings with unknown vocabulary: " + ", ".join(offenders)


@pytest.mark.contract
def test_every_binding_references_a_known_cdm_table() -> None:
    """Every :class:`ColumnBinding` must reference a registered CDM table."""
    known_cdms = set(registered_cdms())
    offenders: list[str] = []
    for b in semantic.all_bindings():
        if b.cdm not in known_cdms:
            offenders.append(f"{b.cdm}.{b.table}.{b.column}: unknown CDM")
            continue
        handle = get_cdm(b.cdm)
        if b.table not in handle.composite_keys:
            offenders.append(f"{b.cdm}.{b.table}.{b.column}: table not in CDM")
    assert not offenders, "Bindings with unknown CDM/table:\n  " + "\n  ".join(offenders)
