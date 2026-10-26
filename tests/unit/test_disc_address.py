"""Unit tests for :class:`ovid2.drivers.disc.DiscAddress`.

Uses ``MagicMock`` for the Spark session so the suite runs without
pyspark. Focused on the CSV reader options — regression coverage for
malformed headers with quoted, whitespace-padded field names.

Post-D3.disc, ``DiscAddress`` lives at
:mod:`ovid2.drivers.disc.legacy`; the classes and semantics are
byte-identical to the pre-refactor ``ovid2.core.addresses.disc``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from ovid2.drivers.disc import DiscAddress


def _make_spark() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Return ``(spark, reader, df)`` mocks wired for the ``read`` path."""
    df = MagicMock(name="DataFrame")
    df.columns = ["externalmedid", "patid"]
    df.toDF.return_value = df

    reader = MagicMock(name="DataFrameReader")
    reader.option.return_value = reader
    reader.csv.return_value = df
    reader.parquet.return_value = df
    reader.json.return_value = df

    spark = MagicMock(name="SparkSession")
    spark.read = reader
    return spark, reader, df


@pytest.mark.unit
def test_read_csv_sets_quote_and_whitespace_options(tmp_path: Path) -> None:
    """CSV reads must set quote, ignoreLeadingWhiteSpace, ignoreTrailingWhiteSpace.

    Regression: ACME 20260610 delivered ``external_meds.csv`` with headers
    like ``"externalmedid", "patid", ...`` — a literal space after every
    comma. Without these three options, Spark preserves the surrounding
    quote characters (and the leading space) as part of the column name,
    and downstream ``selectExpr("patid AS PATID")`` fails with
    ``AnalysisException: Column 'patid' does not exist`` because the
    real column is ``" patid"`` or ``"\\"patid\\""``.
    """
    csv_file = tmp_path / "external_meds.csv"
    csv_file.write_text('"externalmedid", "patid"\n1, p1\n', encoding="utf-8")
    addr = DiscAddress({"path": str(tmp_path), "format": "csv"})

    spark, reader, _ = _make_spark()
    addr.read(spark, pipeline="pipeline", batch_id="20260610", file_name="external_meds.csv")

    option_calls = reader.option.call_args_list
    assert call("quote", '"') in option_calls
    assert call("ignoreLeadingWhiteSpace", "true") in option_calls
    assert call("ignoreTrailingWhiteSpace", "true") in option_calls
    assert call("header", "true") in option_calls
    assert call("inferSchema", "false") in option_calls


@pytest.mark.unit
def test_read_csv_passes_detected_delimiter(tmp_path: Path) -> None:
    """The delimiter option is populated from either the caller or auto-detect."""
    csv_file = tmp_path / "sample.csv"
    csv_file.write_text("a|b|c\n1|2|3\n", encoding="utf-8")
    addr = DiscAddress({"path": str(tmp_path), "format": "csv"})

    spark, reader, _ = _make_spark()
    addr.read(spark, pipeline="pipeline", batch_id="20260610", file_name="sample.csv")

    option_calls = reader.option.call_args_list
    assert call("delimiter", "|") in option_calls


@pytest.mark.unit
def test_read_parquet_skips_csv_options(tmp_path: Path) -> None:
    """Parquet reads do not need quote/whitespace options."""
    parquet_path = tmp_path / "sample.parquet"
    parquet_path.write_bytes(b"PAR1")  # not a real parquet, but the read path is mocked
    addr = DiscAddress({"path": str(tmp_path), "format": "parquet"})

    spark, reader, _ = _make_spark()
    addr.read(spark, pipeline="pipeline", batch_id="20260610", file_name="sample.parquet")

    reader.parquet.assert_called_once()
    # Whitespace options are CSV-only.
    option_calls = reader.option.call_args_list
    assert call("ignoreLeadingWhiteSpace", "true") not in option_calls


# --------------------------------------------------------------------------
# resolve_read_path — the address is the exact location (no pipeline/batch)
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_read_path_reads_directly_under_address(tmp_path: Path) -> None:
    """Files resolve directly under the address directory."""
    from ovid2.drivers.disc.spark import resolve_read_path

    target = tmp_path / "person.csv"
    target.write_text("a\n1\n", encoding="utf-8")

    assert resolve_read_path(str(tmp_path), "person.csv") == str(target)


@pytest.mark.unit
def test_resolve_read_path_never_descends_into_pipeline_or_batch(tmp_path: Path) -> None:
    """A ``{pipeline}/{batch}`` sub-tree is NOT searched — the address is exact."""
    from ovid2.drivers.disc.spark import resolve_read_path

    nested = tmp_path / "acme" / "20260610" / "person.csv"
    nested.parent.mkdir(parents=True)
    nested.write_text("a\n1\n", encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        resolve_read_path(str(tmp_path), "person.csv")
    # Pointing the address at the batch folder itself is how it is read.
    assert resolve_read_path(str(nested.parent), "person.csv") == str(nested)


@pytest.mark.unit
def test_resolve_read_path_glob_prefers_files(tmp_path: Path) -> None:
    """Globs prefer a file match over a directory match."""
    from ovid2.drivers.disc.spark import resolve_read_path

    (tmp_path / "a_datavant_person_tokens_dir").mkdir()
    target = tmp_path / "b_datavant_person_tokens.csv"
    target.write_text("a\n1\n", encoding="utf-8")

    assert resolve_read_path(str(tmp_path), "*datavant_person_tokens*") == str(target)


@pytest.mark.unit
def test_resolve_read_path_glob_falls_back_to_directory(tmp_path: Path) -> None:
    """A glob matching only a directory resolves to it (Spark reads dirs)."""
    from ovid2.drivers.disc.spark import resolve_read_path

    target = tmp_path / "demographic.csv"
    target.mkdir()

    assert resolve_read_path(str(tmp_path), "demographic*") == str(target)


@pytest.mark.unit
def test_resolve_read_path_missing_raises_naming_address(tmp_path: Path) -> None:
    """Missing file → FileNotFoundError naming the exact address directory."""
    from ovid2.drivers.disc.spark import resolve_read_path

    with pytest.raises(FileNotFoundError) as excinfo:
        resolve_read_path(str(tmp_path), "person.csv")
    assert str(tmp_path) in str(excinfo.value)


@pytest.mark.unit
def test_write_path_is_address_directory(tmp_path: Path) -> None:
    """``_get_write_path`` ignores pipeline / batch — the address is exact."""
    addr = DiscAddress({"path": str(tmp_path / "out")})
    assert addr._get_write_path("demo", "20260101") == str(tmp_path / "out")
