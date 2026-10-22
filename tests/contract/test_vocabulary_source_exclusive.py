"""Contract: every :class:`VocabularyHandle` sets exactly one source leg (C1).

Phase C1 widened :class:`ovid2.semantic.handle.VocabularyHandle` from
two source legs (``source_path``, ``shared_view``) to four
(``source_path``, ``shared_view``, ``address``, ``inline_items``). This
test ensures the invariant that exactly one leg is set — the dataclass'
own ``__post_init__`` raises on violation, so this contract also serves
as a smoke test that no bundled handle is constructed in an invalid
state.

The address leg additionally requires an ``address_table`` — a
vocabulary hosted in Snowflake without knowing which table to read
would be useless.
"""

from __future__ import annotations

import pytest

import ovid2.semantic as semantic
from ovid2.semantic.handle import VocabularyHandle


_LEG_NAMES: frozenset[str] = frozenset({
    "source_path",
    "shared_view",
    "address",
    "inline_items",
})


@pytest.mark.contract
def test_every_vocabulary_sets_exactly_one_source_leg() -> None:
    """No handle has multiple legs or zero legs."""
    offenders: list[str] = []
    for name in semantic.registered("vocabulary"):
        handle = semantic.get_vocabulary(name)
        legs = _which_legs(handle)
        if len(legs) != 1:
            offenders.append(f"{name}: {sorted(legs)}")
    assert not offenders, "Vocabularies with wrong leg count:\n  " + "\n  ".join(offenders)


@pytest.mark.contract
def test_source_leg_helper_matches_reality() -> None:
    """``source_leg()`` returns the one leg that is set."""
    offenders: list[str] = []
    for name in semantic.registered("vocabulary"):
        handle = semantic.get_vocabulary(name)
        declared = handle.source_leg()
        actual = _which_legs(handle)
        if declared not in actual:
            offenders.append(f"{name}: declared={declared} actual={sorted(actual)}")
    assert not offenders, "source_leg() disagrees with reality:\n  " + "\n  ".join(offenders)


@pytest.mark.contract
def test_address_leg_requires_address_table() -> None:
    """A vocabulary with ``address`` set must also set ``address_table``."""
    offenders: list[str] = []
    for name in semantic.registered("vocabulary"):
        handle = semantic.get_vocabulary(name)
        if handle.address is not None and not handle.address_table:
            offenders.append(name)
    assert not offenders, "Address-backed vocabs missing address_table: " + ", ".join(offenders)


@pytest.mark.contract
def test_construction_rejects_multiple_source_legs() -> None:
    """Direct construction with two legs must raise."""
    with pytest.raises(ValueError, match="cannot set multiple source legs"):
        VocabularyHandle(
            name="test_multi_leg",
            code_column="code",
            source_path=None,  # placeholder — combining shared_view + inline_items instead
            inline_items=(("A", None),),
            address="some_addr",
            address_table="t",
        )


@pytest.mark.contract
def test_construction_rejects_zero_source_legs() -> None:
    """Direct construction with no legs must raise."""
    with pytest.raises(ValueError, match="must set exactly one of"):
        VocabularyHandle(name="test_no_leg", code_column="code")


def _which_legs(handle: VocabularyHandle) -> set[str]:
    """Return the set of source legs a handle has set."""
    legs: set[str] = set()
    if handle.source_path is not None:
        legs.add("source_path")
    if handle.shared_view is not None:
        legs.add("shared_view")
    if handle.address is not None:
        legs.add("address")
    if handle.inline_items:
        legs.add("inline_items")
    return legs
