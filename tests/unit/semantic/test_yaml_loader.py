"""Unit tests for :mod:`ovid2.semantic.vocabularies._yaml_loader` (S3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ovid2.semantic.handle import VocabularyHandle
from ovid2.semantic.registry import ColumnBinding
from ovid2.semantic.vocabularies import _yaml_loader as loader

# ---------------------------------------------------------------------------
# build_vocabulary
# ---------------------------------------------------------------------------


class TestBuildVocabulary:
    """Cover the four source legs + validation paths."""

    def test_source_path_leg(self) -> None:
        payload = {
            "v": 1,
            "kind": "vocabulary",
            "name": "loinc",
            "code_column": "concept_code",
            "source_path": "/tmp/loinc.csv",
        }
        h = loader.build_vocabulary(payload)
        assert isinstance(h, VocabularyHandle)
        assert h.name == "loinc"
        assert h.source_leg() == "source_path"
        assert h.source_path == Path("/tmp/loinc.csv")

    def test_source_path_expands_omop_cdm_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # settings.OMOP_CDM_DIR is read via the loader's local import.
        from ovid2.config import settings

        monkeypatch.setattr(settings, "OMOP_CDM_DIR", Path("/data/omop"))
        payload = {
            "v": 1,
            "kind": "vocabulary",
            "name": "snomed",
            "code_column": "concept_code",
            "source_path": "${OMOP_CDM_DIR}/vocab_slices/snomed.csv",
        }
        h = loader.build_vocabulary(payload)
        assert h.source_path == Path("/data/omop/vocab_slices/snomed.csv")

    def test_source_path_expands_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_SLICE_HOME", "/scratch")
        payload = {
            "v": 1,
            "kind": "vocabulary",
            "name": "loinc",
            "code_column": "concept_code",
            "source_path": "$MY_SLICE_HOME/loinc.csv",
        }
        h = loader.build_vocabulary(payload)
        assert h.source_path == Path("/scratch/loinc.csv")

    def test_inline_items_leg(self) -> None:
        payload = {
            "v": 1,
            "kind": "vocabulary",
            "name": "tiny_enum",
            "code_column": "code",
            "inline_items": [
                {"code": "A", "label": "Alpha"},
                {"code": "B"},
            ],
        }
        h = loader.build_vocabulary(payload)
        assert h.source_leg() == "inline_items"
        assert h.inline_items == (("A", "Alpha"), ("B", None))

    def test_address_leg(self) -> None:
        payload = {
            "v": 1,
            "kind": "vocabulary",
            "name": "ref_hosted",
            "code_column": "concept_code",
            "address": "vocab_snowflake",
            "address_table": "VOCAB.LOINC",
        }
        h = loader.build_vocabulary(payload)
        assert h.source_leg() == "address"
        assert h.address == "vocab_snowflake"
        assert h.address_table == "VOCAB.LOINC"

    def test_shared_view_object_form_resolves(self) -> None:
        # OMOP CONCEPT ships with `shared_view: {cdm: pcornet_7_0, view: concept}`.
        # importing ovid2.semantic registers it, but build_vocabulary is
        # deterministic: call it with the same payload and expect a
        # resolved SharedViewSpec.
        import ovid2.semantic  # noqa: F401 — ensure CDMs are loaded.

        payload = {
            "v": 1,
            "kind": "vocabulary",
            "name": "omop_concept_alt",
            "code_column": "concept_code",
            "shared_view": {"cdm": "pcornet_7_0", "view": "concept"},
        }
        h = loader.build_vocabulary(payload)
        assert h.source_leg() == "shared_view"
        assert h.shared_view is not None
        assert h.shared_view.view_name == "concept"

    def test_shared_view_string_form_rejected(self) -> None:
        payload = {
            "v": 1,
            "kind": "vocabulary",
            "name": "old",
            "code_column": "concept_code",
            "shared_view": "concept",
        }
        with pytest.raises(ValueError, match="bare-string 'shared_view' is not yet supported"):
            loader.build_vocabulary(payload)

    def test_shared_view_unknown_cdm(self) -> None:
        payload = {
            "v": 1,
            "kind": "vocabulary",
            "name": "bad",
            "code_column": "concept_code",
            "shared_view": {"cdm": "no_such_cdm", "view": "concept"},
        }
        with pytest.raises(ValueError, match="unknown CDM"):
            loader.build_vocabulary(payload)

    def test_shared_view_unknown_view(self) -> None:
        import ovid2.semantic  # noqa: F401

        payload = {
            "v": 1,
            "kind": "vocabulary",
            "name": "bad",
            "code_column": "concept_code",
            "shared_view": {"cdm": "pcornet_7_0", "view": "no_such_view"},
        }
        with pytest.raises(ValueError, match="no shared view"):
            loader.build_vocabulary(payload)

    def test_missing_name_raises(self) -> None:
        with pytest.raises(ValueError, match="missing required 'name'"):
            loader.build_vocabulary({"v": 1, "kind": "vocabulary", "code_column": "c"})

    def test_missing_code_column_raises(self) -> None:
        with pytest.raises(ValueError, match="missing 'code_column'"):
            loader.build_vocabulary({"v": 1, "kind": "vocabulary", "name": "x"})

    def test_wrong_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="kind must be 'vocabulary'"):
            loader.build_vocabulary({"v": 1, "kind": "metric", "name": "x", "code_column": "c"})

    def test_tags_are_tuple(self) -> None:
        payload = {
            "v": 1,
            "kind": "vocabulary",
            "name": "loinc",
            "code_column": "concept_code",
            "source_path": "/tmp/loinc.csv",
            "tags": ["clinical", "lab"],
        }
        h = loader.build_vocabulary(payload)
        assert h.tags == ("clinical", "lab")


# ---------------------------------------------------------------------------
# build_binding
# ---------------------------------------------------------------------------


class TestBuildBinding:
    """Cover the binding builder's required-field + upper-casing behaviour."""

    def test_minimal_row(self) -> None:
        b = loader.build_binding(
            "pcornet_7_0",
            {"table": "diagnosis", "column": "DX", "vocabulary": "icd10cm"},
        )
        assert isinstance(b, ColumnBinding)
        assert b.cdm == "pcornet_7_0"
        assert b.table == "diagnosis"
        assert b.column == "DX"
        assert b.vocabulary == "icd10cm"
        assert b.role is None
        assert b.required is False

    def test_full_row(self) -> None:
        b = loader.build_binding(
            "pcornet_7_0",
            {
                "table": "lab_result_cm",
                "column": "LAB_LOINC",
                "vocabulary": "loinc",
                "role": "lab_result_loinc",
                "required": True,
            },
        )
        assert b.role == "lab_result_loinc"
        assert b.required is True

    def test_column_is_upper_cased(self) -> None:
        b = loader.build_binding(
            "pcornet_7_0",
            {"table": "diagnosis", "column": "dx", "vocabulary": "icd10cm"},
        )
        assert b.column == "DX"

    def test_missing_table_raises(self) -> None:
        with pytest.raises(ValueError, match="missing 'table'"):
            loader.build_binding("cdm", {"column": "X", "vocabulary": "v"})

    def test_missing_column_raises(self) -> None:
        with pytest.raises(ValueError, match="missing 'column'"):
            loader.build_binding("cdm", {"table": "t", "vocabulary": "v"})

    def test_missing_vocabulary_raises(self) -> None:
        with pytest.raises(ValueError, match="missing 'vocabulary'"):
            loader.build_binding("cdm", {"table": "t", "column": "X"})


# ---------------------------------------------------------------------------
# iter_* + register_all
# ---------------------------------------------------------------------------


class TestIterAndRegister:
    """Cover discovery + registration end-to-end against the packaged tree."""

    def test_iter_vocabulary_docs_finds_bundled_files(self) -> None:
        docs = loader.iter_vocabulary_docs()
        stems = {stem for stem, _ in docs}
        # The six OMOP-derived vocabularies + OMOP CONCEPT.
        assert {"omop_concept", "loinc", "snomed", "icd10cm", "icd9cm", "rxnorm", "ndc"} <= stems

    def test_iter_binding_docs_finds_bundled_files(self) -> None:
        docs = loader.iter_binding_docs()
        cdms = {cdm for cdm, _ in docs}
        assert "pcornet_7_0" in cdms

    def test_register_all_is_idempotent(self) -> None:
        # ovid2.semantic import already ran register_all once. Calling it
        # again must not raise.
        loader.register_all()
        loader.register_all()

    def test_iter_vocabulary_docs_skips_non_yaml_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Point the loader at a synthetic directory containing one YAML
        # and one non-YAML file; only the YAML is returned.
        (tmp_path / "good.yaml").write_text(
            "v: 1\nkind: vocabulary\nname: good\ncode_column: c\nsource_path: /tmp/x\n"
        )
        (tmp_path / "README.md").write_text("ignore me\n")
        monkeypatch.setattr(loader, "_vocabularies_dir", lambda: tmp_path)
        docs = loader.iter_vocabulary_docs()
        assert [stem for stem, _ in docs] == ["good"]

    def test_iter_binding_docs_warns_on_mismatched_cdm(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        (tmp_path / "pcornet_7_0.yaml").write_text(
            "v: 1\ncdm: something_else\nbindings:\n- {table: t, column: X, vocabulary: v}\n"
        )
        monkeypatch.setattr(loader, "_bindings_dir", lambda: tmp_path)
        with caplog.at_level("WARNING", logger="ovid2.semantic.vocabularies._yaml_loader"):
            docs = loader.iter_binding_docs()
        # The document is still returned (uses the YAML's cdm value),
        # but a warning is emitted about the filename mismatch.
        assert [cdm for cdm, _ in docs] == ["something_else"]
        assert any("does not match" in rec.message for rec in caplog.records)

    def test_malformed_yaml_is_skipped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        (tmp_path / "broken.yaml").write_text(": : : not yaml\n")
        (tmp_path / "ok.yaml").write_text(
            "v: 1\nkind: vocabulary\nname: ok\ncode_column: c\nsource_path: /tmp/y\n"
        )
        monkeypatch.setattr(loader, "_vocabularies_dir", lambda: tmp_path)
        with caplog.at_level("WARNING", logger="ovid2.semantic.vocabularies._yaml_loader"):
            docs = loader.iter_vocabulary_docs()
        assert [stem for stem, _ in docs] == ["ok"]
        assert any("Skipping" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestPathHelpers:
    """Cover the path-resolution utility surface."""

    def test_semantic_yaml_root_points_at_resources(self) -> None:
        root = loader.semantic_yaml_root()
        assert root.is_dir()
        assert root.name == "semantic"

    def test_vocabularies_directory_contains_yamls(self) -> None:
        directory = loader._vocabularies_dir()
        assert directory.is_dir()
        assert any(p.suffix == ".yaml" for p in directory.iterdir())
