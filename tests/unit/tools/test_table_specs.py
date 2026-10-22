"""Unit tests for :mod:`ovid2.tools.table_specs`.

The parser turns each raw ``--tables`` CLI entry into a
:class:`TableSpec` with two fields:

- ``table_name`` — logical / destination name (used verbatim as the
  destination file stem, case preserved).
- ``file_name`` — source-side lookup key (defaults to ``table_name``
  when the operator does not supply an explicit ``=SOURCE``).

The full contract is exercised here so tests in ``test_copy.py`` /
``test_move.py`` can lean on the parser without re-testing its edge
cases.
"""

from __future__ import annotations

import logging

import pytest

from ovid2.tools.table_specs import (
    TableSpec,
    TableSpecError,
    is_all_sentinel,
    parse_table_spec,
    parse_table_specs,
)


class TestAllSentinel:
    def test_single_all_is_sentinel(self) -> None:
        assert is_all_sentinel(["all"]) is True

    def test_case_insensitive(self) -> None:
        assert is_all_sentinel(["ALL"]) is True
        assert is_all_sentinel(["All"]) is True

    def test_whitespace_padding_ignored(self) -> None:
        assert is_all_sentinel(["  all "]) is True

    def test_all_with_other_tables_is_not_sentinel(self) -> None:
        assert is_all_sentinel(["all", "DEMOGRAPHIC"]) is False

    def test_empty_is_not_sentinel(self) -> None:
        assert is_all_sentinel([]) is False
        assert is_all_sentinel([""]) is False

    def test_explicit_list_is_not_sentinel(self) -> None:
        assert is_all_sentinel(["DEMOGRAPHIC", "ENCOUNTER"]) is False


class TestSingleFieldForm:
    """A bare ``LOGICAL`` token defaults ``file_name`` to ``table_name``."""

    def test_bare_name_defaults_source(self) -> None:
        got = parse_table_spec("HASH_TOKEN")
        assert got == TableSpec(table_name="HASH_TOKEN", file_name="HASH_TOKEN")

    def test_lowercase_preserved(self) -> None:
        got = parse_table_spec("hash_token")
        assert got.table_name == "hash_token"
        assert got.file_name == "hash_token"

    def test_mixed_case_preserved(self) -> None:
        got = parse_table_spec("Hash_Token")
        assert got.table_name == "Hash_Token"
        assert got.file_name == "Hash_Token"

    def test_leading_trailing_whitespace_stripped(self) -> None:
        got = parse_table_spec("   HASH_TOKEN   ")
        assert got.table_name == "HASH_TOKEN"
        assert got.file_name == "HASH_TOKEN"


class TestSplitForm:
    """``LOGICAL=SOURCE`` yields distinct table_name / file_name."""

    def test_glob_source(self) -> None:
        got = parse_table_spec("HASH_TOKEN=*datavant_person_tokens*")
        assert got.table_name == "HASH_TOKEN"
        assert got.file_name == "*datavant_person_tokens*"

    def test_extension_source(self) -> None:
        got = parse_table_spec("HASH_TOKEN=hash_token.txt.csv")
        assert got.table_name == "HASH_TOKEN"
        assert got.file_name == "hash_token.txt.csv"

    def test_internal_whitespace_stripped_on_both_sides(self) -> None:
        got = parse_table_spec("  HASH_TOKEN  =  hash_token.csv  ")
        assert got.table_name == "HASH_TOKEN"
        assert got.file_name == "hash_token.csv"

    def test_case_preserved_on_both_sides(self) -> None:
        got = parse_table_spec("HASH_TOKEN=Hash_Token.csv")
        assert got.table_name == "HASH_TOKEN"
        assert got.file_name == "Hash_Token.csv"


class TestValidation:
    """Malformed entries raise :class:`TableSpecError`."""

    def test_empty_string(self) -> None:
        with pytest.raises(TableSpecError):
            parse_table_spec("")

    def test_whitespace_only(self) -> None:
        with pytest.raises(TableSpecError):
            parse_table_spec("   ")

    def test_empty_logical_half(self) -> None:
        with pytest.raises(TableSpecError, match="empty LOGICAL"):
            parse_table_spec("=source_file.csv")

    def test_empty_source_half(self) -> None:
        with pytest.raises(TableSpecError, match="empty SOURCE"):
            parse_table_spec("HASH_TOKEN=")

    def test_multiple_equals_rejected(self) -> None:
        with pytest.raises(TableSpecError, match="more than one"):
            parse_table_spec("A=B=C")


class TestGlobInLogicalWarnsButRuns:
    """Glob metachars in LOGICAL log a warning; parsing still succeeds.

    The rule the ADR codifies is that LOGICAL is used verbatim as the
    destination file stem. If an operator asks for that literal, we
    honour it — but log so the mistake is discoverable.
    """

    def test_glob_metachar_in_logical_logs_warning(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="ovid2.tools.table_specs"):
            got = parse_table_spec("*bad*")
        assert got.table_name == "*bad*"
        assert any("glob metacharacters" in r.message for r in caplog.records)

    def test_square_bracket_treated_as_glob(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="ovid2.tools.table_specs"):
            parse_table_spec("A[1]")
        assert any("glob metacharacters" in r.message for r in caplog.records)

    def test_glob_in_source_side_is_fine(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A glob on the SOURCE side is the whole point — no warning."""
        with caplog.at_level(logging.WARNING, logger="ovid2.tools.table_specs"):
            parse_table_spec("HASH_TOKEN=*datavant*")
        assert not [r for r in caplog.records if "glob" in r.message]


class TestBatchParse:
    """``parse_table_specs`` preserves order and mixes forms."""

    def test_mixed_forms_preserve_order(self) -> None:
        got = parse_table_specs(
            [
                "DEMOGRAPHIC",
                "HASH_TOKEN=*datavant_person_tokens*",
                "ENCOUNTER",
            ]
        )
        assert [s.table_name for s in got] == [
            "DEMOGRAPHIC",
            "HASH_TOKEN",
            "ENCOUNTER",
        ]
        assert [s.file_name for s in got] == [
            "DEMOGRAPHIC",
            "*datavant_person_tokens*",
            "ENCOUNTER",
        ]

    def test_first_bad_entry_raises(self) -> None:
        with pytest.raises(TableSpecError):
            parse_table_specs(["OK", "=bad", "ALSO_OK"])

    def test_empty_input(self) -> None:
        assert parse_table_specs([]) == []


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
