"""Unit tests for :class:`ovid2.semantic.handle.VocabularyHandle` invariants."""

from __future__ import annotations

from pathlib import Path

import pytest

from ovid2.cdm.handle import SharedViewSpec
from ovid2.semantic.handle import VocabularyHandle


class TestVocabularyHandleConstruction:
    """Frozen-dataclass invariants that ``__post_init__`` enforces."""

    def test_source_path_only_is_valid(self, tmp_path: Path) -> None:
        """A vocabulary can be sourced from a filesystem CSV alone."""
        h = VocabularyHandle(
            name="loinc",
            code_column="loinc_num",
            source_path=tmp_path / "loinc.csv",
        )
        assert h.resolved_source_path() == tmp_path / "loinc.csv"
        assert h.resolved_view_name() == "loinc"

    def test_shared_view_only_is_valid(self, tmp_path: Path) -> None:
        """A vocabulary can wrap an already-registered shared view."""
        view = SharedViewSpec(view_name="concept", source_path=tmp_path / "CONCEPT.csv")
        h = VocabularyHandle(name="omop_concept", code_column="concept_code", shared_view=view)
        assert h.resolved_view_name() == "concept"
        assert h.resolved_source_path() == tmp_path / "CONCEPT.csv"

    def test_view_name_defaults_to_name(self, tmp_path: Path) -> None:
        """When view_name is empty it should default to the handle name."""
        h = VocabularyHandle(
            name="snomed_ct",
            code_column="code",
            source_path=tmp_path / "snomed.csv",
        )
        assert h.view_name == "snomed_ct"

    def test_explicit_view_name_wins(self, tmp_path: Path) -> None:
        """Callers can override the derived default."""
        h = VocabularyHandle(
            name="snomed_ct",
            code_column="code",
            source_path=tmp_path / "snomed.csv",
            view_name="snomed",
        )
        assert h.view_name == "snomed"

    def test_neither_source_nor_shared_view_raises(self) -> None:
        """A vocabulary with no source is meaningless — construction must fail."""
        with pytest.raises(ValueError, match="must set exactly one of"):
            VocabularyHandle(name="loinc", code_column="loinc_num")

    def test_both_source_and_shared_view_raises(self, tmp_path: Path) -> None:
        """Ambiguous source specification is rejected up front."""
        view = SharedViewSpec(view_name="concept", source_path=tmp_path / "CONCEPT.csv")
        with pytest.raises(ValueError, match="cannot set multiple source legs"):
            VocabularyHandle(
                name="loinc",
                code_column="loinc_num",
                source_path=tmp_path / "loinc.csv",
                shared_view=view,
            )

    def test_missing_code_column_raises(self, tmp_path: Path) -> None:
        """Every vocabulary must declare which column carries the code value."""
        with pytest.raises(ValueError, match="code_column"):
            VocabularyHandle(name="loinc", source_path=tmp_path / "loinc.csv")

    def test_kind_is_vocabulary(self, tmp_path: Path) -> None:
        """The class-level discriminator must be set correctly."""
        h = VocabularyHandle(
            name="loinc",
            code_column="loinc_num",
            source_path=tmp_path / "loinc.csv",
        )
        assert h.kind == "vocabulary"

    def test_is_frozen(self, tmp_path: Path) -> None:
        """Handles are immutable once constructed."""
        h = VocabularyHandle(
            name="loinc",
            code_column="loinc_num",
            source_path=tmp_path / "loinc.csv",
        )
        with pytest.raises((AttributeError, Exception)):
            h.name = "changed"  # type: ignore[misc]
