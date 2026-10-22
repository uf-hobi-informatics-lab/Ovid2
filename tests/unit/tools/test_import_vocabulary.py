"""Unit tests for ``ovid2 tools import-vocabulary`` (S3)."""

from __future__ import annotations

import argparse
import io
from pathlib import Path

import pytest
import yaml

from ovid2.tools import import_vocabulary as iv


# ---------------------------------------------------------------------------
# Synthetic CONCEPT.csv fixtures
# ---------------------------------------------------------------------------


CONCEPT_HEADER = (
    "concept_id\tconcept_name\tdomain_id\tvocabulary_id\tconcept_class_id\t"
    "standard_concept\tconcept_code\tvalid_start_date\tvalid_end_date\tinvalid_reason\n"
)


def _write_concept(dir_: Path, rows: list[tuple[str, str]]) -> Path:
    """Write a minimal CONCEPT.csv containing ``rows`` (vocabulary_id, code)."""
    concept_path = dir_ / "CONCEPT.csv"
    with concept_path.open("w", encoding="utf-8") as fh:
        fh.write(CONCEPT_HEADER)
        for i, (vid, code) in enumerate(rows, start=1):
            fh.write(
                f"{i}\tsample_{code}\tDrug\t{vid}\tClinical\tS\t{code}\t"
                f"2020-01-01\t2099-12-31\t\n"
            )
    return concept_path


def _write_vocabulary_csv(dir_: Path, versions: dict[str, str]) -> Path:
    """Write a minimal VOCABULARY.csv keyed by vocabulary_id → version."""
    path = dir_ / "VOCABULARY.csv"
    with path.open("w", encoding="utf-8") as fh:
        fh.write("vocabulary_id\tvocabulary_name\tvocabulary_reference\tvocabulary_version\tvocabulary_concept_id\n")
        for vid, ver in versions.items():
            fh.write(f"{vid}\tname\turl\t{ver}\t0\n")
    return path


# ---------------------------------------------------------------------------
# Spec catalogue
# ---------------------------------------------------------------------------


class TestVocabCatalogue:
    """The static _KNOWN_VOCABS list is stable and complete."""

    def test_known_slugs_match_expected_set(self) -> None:
        assert set(iv._KNOWN_SLUGS) == {
            "loinc", "snomed", "icd10cm", "icd9cm", "rxnorm", "ndc",
        }

    def test_spec_lookup_by_slug(self) -> None:
        spec = iv._spec_by_slug("loinc")
        assert spec is not None
        assert spec.vocabulary_id == "LOINC"

    def test_unknown_slug_returns_none(self) -> None:
        assert iv._spec_by_slug("nope") is None

    def test_vocabulary_ids_are_case_sensitive(self) -> None:
        # RxNorm in the OMOP bundle uses mixed case; verify our spec matches.
        rxnorm = iv._spec_by_slug("rxnorm")
        assert rxnorm is not None
        assert rxnorm.vocabulary_id == "RxNorm"


# ---------------------------------------------------------------------------
# CONCEPT.csv reader
# ---------------------------------------------------------------------------


class TestOpenConceptCsv:
    """CONCEPT.csv discovery accepts common filename casings."""

    def test_uppercase_default(self, tmp_path: Path) -> None:
        _write_concept(tmp_path, [("LOINC", "1234-5")])
        assert iv._open_concept_csv(tmp_path).name == "CONCEPT.csv"

    def test_lowercase(self, tmp_path: Path) -> None:
        (tmp_path / "concept.csv").write_text(CONCEPT_HEADER)
        assert iv._open_concept_csv(tmp_path).name == "concept.csv"

    def test_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            iv._open_concept_csv(tmp_path)


class TestReadVocabularyVersions:
    """VOCABULARY.csv → {vocabulary_id: version} extraction is robust."""

    def test_happy_path(self, tmp_path: Path) -> None:
        _write_vocabulary_csv(tmp_path, {"LOINC": "2.78", "NDC": "20250223"})
        result = iv._read_vocabulary_versions(tmp_path)
        assert result == {"LOINC": "2.78", "NDC": "20250223"}

    def test_missing_returns_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level("WARNING", logger="ovid2.tools.import_vocabulary"):
            result = iv._read_vocabulary_versions(tmp_path)
        assert result == {}
        assert any("No VOCABULARY.csv" in rec.message for rec in caplog.records)

    def test_missing_columns_returns_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        (tmp_path / "VOCABULARY.csv").write_text("id\tname\n1\tLOINC\n")
        with caplog.at_level("WARNING", logger="ovid2.tools.import_vocabulary"):
            result = iv._read_vocabulary_versions(tmp_path)
        assert result == {}


# ---------------------------------------------------------------------------
# Slicer
# ---------------------------------------------------------------------------


class TestSliceConceptCsv:
    """Streaming slice-by-vocab_id preserves header and counts rows correctly."""

    def test_counts_only_dry_run(self, tmp_path: Path) -> None:
        concept = _write_concept(
            tmp_path,
            [("LOINC", "1"), ("SNOMED", "2"), ("LOINC", "3"), ("NDC", "4")],
        )
        counts = iv._slice_concept_csv(concept, {"LOINC", "NDC"}, outputs=None)
        assert counts == {"LOINC": 2, "NDC": 1}

    def test_writes_rows_when_outputs_provided(self, tmp_path: Path) -> None:
        concept = _write_concept(
            tmp_path,
            [("LOINC", "1"), ("SNOMED", "2"), ("LOINC", "3")],
        )
        loinc_out = io.StringIO()
        counts = iv._slice_concept_csv(concept, {"LOINC"}, outputs={"LOINC": loinc_out})
        assert counts == {"LOINC": 2}
        written = loinc_out.getvalue().splitlines()
        # Header + 2 rows.
        assert len(written) == 3
        assert written[0].startswith("concept_id\tconcept_name")
        assert "LOINC" in written[1]

    def test_ignores_unwanted_vocab_ids(self, tmp_path: Path) -> None:
        concept = _write_concept(tmp_path, [("SNOMED", "1"), ("SNOMED", "2")])
        counts = iv._slice_concept_csv(concept, {"LOINC"}, outputs=None)
        assert counts == {"LOINC": 0}

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        (tmp_path / "CONCEPT.csv").write_text("")
        with pytest.raises(ValueError, match="empty file"):
            iv._slice_concept_csv(tmp_path / "CONCEPT.csv", {"LOINC"}, outputs=None)

    def test_missing_vocabulary_id_column_raises(self, tmp_path: Path) -> None:
        (tmp_path / "CONCEPT.csv").write_text("id\tname\n1\tfoo\n")
        with pytest.raises(ValueError, match="'vocabulary_id' column"):
            iv._slice_concept_csv(tmp_path / "CONCEPT.csv", {"LOINC"}, outputs=None)


# ---------------------------------------------------------------------------
# YAML rendering
# ---------------------------------------------------------------------------


class TestRenderVocabYaml:
    """Emitted YAML has the shape the S3 loader expects."""

    def test_shape_contains_required_fields(self) -> None:
        spec = iv._spec_by_slug("loinc")
        assert spec is not None
        payload = iv._render_vocab_yaml(spec, "LOINC 2.78")
        assert payload["v"] == 1
        assert payload["kind"] == "vocabulary"
        assert payload["name"] == "loinc"
        assert payload["version"] == "LOINC 2.78"
        assert payload["code_column"] == "concept_code"
        assert payload["source_path"] == "${OMOP_CDM_DIR}/vocab_slices/loinc.csv"
        assert "clinical" in payload["tags"]

    def test_empty_version_falls_back(self) -> None:
        spec = iv._spec_by_slug("rxnorm")
        assert spec is not None
        payload = iv._render_vocab_yaml(spec, "")
        assert payload["version"] == "OMOP 5.3 slice"

    def test_yaml_roundtrips_through_dump_load(self) -> None:
        spec = iv._spec_by_slug("ndc")
        assert spec is not None
        payload = iv._render_vocab_yaml(spec, "NDC 20250223")
        payload.pop("_generated_at", None)
        text = iv._dump_yaml(payload)
        loaded = yaml.safe_load(text)
        assert loaded["name"] == "ndc"
        assert loaded["version"] == "NDC 20250223"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _make_args(**overrides: object) -> argparse.Namespace:
    """Build a Namespace mirroring the argparse output for ``main``."""
    defaults: dict[str, object] = {
        "source": "/nonexistent",
        "vocab": ["loinc"],
        "slice_dir": "/nonexistent/slices",
        "out_dir": "/nonexistent/yamls",
        "write": False,
        "allow_empty": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestMain:
    """End-to-end CLI behaviour on synthetic OMOP bundles."""

    def test_missing_source_dir(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        args = _make_args(source=str(tmp_path / "no_such_dir"))
        rc = iv.main(args)
        assert rc == 2
        assert "not a directory" in capsys.readouterr().err

    def test_missing_concept_csv(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        args = _make_args(source=str(tmp_path))
        rc = iv.main(args)
        assert rc == 2
        assert "No CONCEPT.csv" in capsys.readouterr().err

    def test_dry_run_reports_counts(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        _write_concept(tmp_path, [("LOINC", "1"), ("LOINC", "2"), ("SNOMED", "3")])
        _write_vocabulary_csv(tmp_path, {"LOINC": "2.78"})
        args = _make_args(
            source=str(tmp_path),
            vocab=["loinc"],
            slice_dir=str(tmp_path / "slices"),
            out_dir=str(tmp_path / "yamls"),
        )
        rc = iv.main(args)
        assert rc == 0
        captured = capsys.readouterr().out
        assert "rows=         2" in captured
        assert "v=2.78" in captured
        # Dry-run must not create output directories.
        assert not (tmp_path / "slices").exists()
        assert not (tmp_path / "yamls").exists()

    def test_write_creates_slice_and_yaml(self, tmp_path: Path) -> None:
        _write_concept(tmp_path, [("LOINC", "1"), ("LOINC", "2")])
        _write_vocabulary_csv(tmp_path, {"LOINC": "2.78"})
        slice_dir = tmp_path / "slices"
        out_dir = tmp_path / "yamls"
        args = _make_args(
            source=str(tmp_path),
            vocab=["loinc"],
            slice_dir=str(slice_dir),
            out_dir=str(out_dir),
            write=True,
        )
        rc = iv.main(args)
        assert rc == 0
        assert (slice_dir / "loinc.csv").is_file()
        assert (out_dir / "loinc.yaml").is_file()
        # Slice preserves header + 2 rows.
        lines = (slice_dir / "loinc.csv").read_text().splitlines()
        assert len(lines) == 3
        # YAML is loadable and points at the expected slice.
        payload = yaml.safe_load((out_dir / "loinc.yaml").read_text())
        assert payload["name"] == "loinc"
        assert payload["source_path"] == "${OMOP_CDM_DIR}/vocab_slices/loinc.csv"
        # No leftover .tmp files.
        assert not (slice_dir / "loinc.csv.tmp").exists()

    def test_empty_slice_fails_by_default(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        _write_concept(tmp_path, [("SNOMED", "1")])  # no LOINC rows
        slice_dir = tmp_path / "slices"
        args = _make_args(
            source=str(tmp_path),
            vocab=["loinc"],
            slice_dir=str(slice_dir),
            out_dir=str(tmp_path / "yamls"),
            write=True,
        )
        rc = iv.main(args)
        assert rc == 1
        assert "refusing to write empty slice" in capsys.readouterr().err
        # Cleanup: no partial .tmp files left behind.
        assert not (slice_dir / "loinc.csv.tmp").exists()

    def test_empty_slice_ok_with_flag(self, tmp_path: Path) -> None:
        _write_concept(tmp_path, [("SNOMED", "1")])
        args = _make_args(
            source=str(tmp_path),
            vocab=["loinc"],
            slice_dir=str(tmp_path / "slices"),
            out_dir=str(tmp_path / "yamls"),
            allow_empty=True,
        )
        rc = iv.main(args)
        assert rc == 0  # dry-run + allow_empty → clean exit

    def test_multiple_vocabs_slice_in_single_pass(self, tmp_path: Path) -> None:
        _write_concept(
            tmp_path,
            [("LOINC", "1"), ("SNOMED", "2"), ("LOINC", "3"), ("NDC", "4")],
        )
        args = _make_args(
            source=str(tmp_path),
            vocab=["loinc", "ndc"],
            slice_dir=str(tmp_path / "slices"),
            out_dir=str(tmp_path / "yamls"),
            write=True,
        )
        rc = iv.main(args)
        assert rc == 0
        assert (tmp_path / "slices" / "loinc.csv").is_file()
        assert (tmp_path / "slices" / "ndc.csv").is_file()
        assert (tmp_path / "yamls" / "loinc.yaml").is_file()
        assert (tmp_path / "yamls" / "ndc.yaml").is_file()
        # LOINC slice has 2 rows + header; NDC slice has 1 row + header.
        assert len((tmp_path / "slices" / "loinc.csv").read_text().splitlines()) == 3
        assert len((tmp_path / "slices" / "ndc.csv").read_text().splitlines()) == 2


# ---------------------------------------------------------------------------
# Argparse registration
# ---------------------------------------------------------------------------


class TestRegister:
    """The subcommand slots into a parser and rejects unknown vocabs."""

    def test_register_creates_subparser(self) -> None:
        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        iv.register(subs)
        args = parser.parse_args([
            "import-vocabulary",
            "--source", "/a",
            "--vocab", "loinc",
            "--slice-dir", "/b",
            "--out-dir", "/c",
        ])
        assert args.source == "/a"
        assert args.vocab == ["loinc"]
        assert args.write is False

    def test_rejects_unknown_vocab(self) -> None:
        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        iv.register(subs)
        with pytest.raises(SystemExit):
            parser.parse_args([
                "import-vocabulary",
                "--source", "/a",
                "--vocab", "totally_fake",
                "--slice-dir", "/b",
                "--out-dir", "/c",
            ])
