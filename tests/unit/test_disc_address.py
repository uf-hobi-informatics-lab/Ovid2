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
    csv_file = tmp_path / "pipeline" / "20260610" / "external_meds.csv"
    csv_file.parent.mkdir(parents=True)
    csv_file.write_text(
        '"externalmedid", "patid"\n1, p1\n', encoding="utf-8"
    )
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
    csv_file = tmp_path / "pipeline" / "20260610" / "sample.csv"
    csv_file.parent.mkdir(parents=True)
    csv_file.write_text("a|b|c\n1|2|3\n", encoding="utf-8")
    addr = DiscAddress({"path": str(tmp_path), "format": "csv"})

    spark, reader, _ = _make_spark()
    addr.read(spark, pipeline="pipeline", batch_id="20260610", file_name="sample.csv")

    option_calls = reader.option.call_args_list
    assert call("delimiter", "|") in option_calls


@pytest.mark.unit
def test_read_parquet_skips_csv_options(tmp_path: Path) -> None:
    """Parquet reads do not need quote/whitespace options."""
    parquet_path = tmp_path / "pipeline" / "20260610" / "sample.parquet"
    parquet_path.parent.mkdir(parents=True)
    parquet_path.write_bytes(b"PAR1")  # not a real parquet, but the read path is mocked
    addr = DiscAddress({"path": str(tmp_path), "format": "parquet"})

    spark, reader, _ = _make_spark()
    addr.read(spark, pipeline="pipeline", batch_id="20260610", file_name="sample.parquet")

    reader.parquet.assert_called_once()
    # Whitespace options are CSV-only.
    option_calls = reader.option.call_args_list
    assert call("ignoreLeadingWhiteSpace", "true") not in option_calls


# --------------------------------------------------------------------------
# resolve_read_path — layout resolution (pipelined + flat fallback, Option 1)
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_read_path_pipelined_layout_wins(tmp_path: Path) -> None:
    """The primary ``{root}/{pipeline}/{batch}`` layout resolves first."""
    from ovid2.drivers.disc.spark import resolve_read_path

    target = tmp_path / "acme_from_disc" / "20260610" / "person.csv"
    target.parent.mkdir(parents=True)
    target.write_text("a\n1\n", encoding="utf-8")

    resolved = resolve_read_path(
        str(tmp_path), "acme_from_disc", "20260610", "person.csv"
    )
    assert resolved == str(target)


@pytest.mark.unit
def test_resolve_read_path_case_insensitive_pipeline_dir(tmp_path: Path) -> None:
    """Upper-cased partner dir matches a lower-cased pipeline name."""
    from ovid2.drivers.disc.spark import resolve_read_path

    target = tmp_path / "ACME" / "20260610" / "person.csv"
    target.parent.mkdir(parents=True)
    target.write_text("a\n1\n", encoding="utf-8")

    resolved = resolve_read_path(str(tmp_path), "acme", "20260610", "person.csv")
    assert resolved == str(target)


@pytest.mark.unit
def test_resolve_read_path_flat_fallback(tmp_path: Path) -> None:
    """When no pipeline-name dir matches, fall back to ``{root}/{batch}``.

    Regression: ACME 20260610 was delivered directly under
    ``/data/inboxarchive/ACME/20260610/`` (the disc root already points at
    the partner dir ``ACME``) while the pipeline is named ``acme_from_disc``.
    The pipelined layout ``.../ACME/acme_from_disc/20260610/`` does not
    exist, so resolution must fall back to the flat batch dir.
    """
    from ovid2.drivers.disc.spark import resolve_read_path

    target = tmp_path / "20260610" / "person.csv"
    target.parent.mkdir(parents=True)
    target.write_text("a\n1\n", encoding="utf-8")

    resolved = resolve_read_path(
        str(tmp_path), "acme_from_disc", "20260610", "person.csv"
    )
    assert resolved == str(target)


@pytest.mark.unit
def test_resolve_read_path_flat_fallback_glob(tmp_path: Path) -> None:
    """The flat fallback honours glob patterns just like the primary layout."""
    from ovid2.drivers.disc.spark import resolve_read_path

    target = tmp_path / "20260610" / "datavant_person_tokens"
    target.parent.mkdir(parents=True)
    target.write_text("a\n1\n", encoding="utf-8")

    resolved = resolve_read_path(
        str(tmp_path), "acme_from_disc", "20260610", "*datavant_person_tokens*"
    )
    assert resolved == str(target)


@pytest.mark.unit
def test_resolve_read_path_empty_pipeline_synthetic(tmp_path: Path) -> None:
    """Empty pipeline/batch collapse to ``{root}/{basename}`` (stager path).

    ``core.pipeline`` builds a synthetic disc driver rooted at a file's
    parent and calls ``resolve_read_path(parent, "", "", basename)``. The
    flat fallback must not regress this: an empty pipeline segment already
    collapses via ``os.path.join``.
    """
    from ovid2.drivers.disc.spark import resolve_read_path

    target = tmp_path / "person.csv"
    target.write_text("a\n1\n", encoding="utf-8")

    resolved = resolve_read_path(str(tmp_path), "", "", "person.csv")
    assert resolved == str(target)


@pytest.mark.unit
def test_resolve_read_path_missing_raises_naming_both_layouts(tmp_path: Path) -> None:
    """Both layouts missing → FileNotFoundError naming both attempts."""
    from ovid2.drivers.disc.spark import resolve_read_path

    with pytest.raises(FileNotFoundError) as excinfo:
        resolve_read_path(str(tmp_path), "acme_from_disc", "20260610", "person.csv")

    message = str(excinfo.value)
    assert "acme_from_disc" in message
    assert "flat layout" in message
    assert f"{tmp_path}/20260610/" in message


@pytest.mark.unit
def test_resolve_read_path_root_direct_batch_baked_in(tmp_path: Path) -> None:
    """Batch id baked into the root → files resolve directly under it.

    Regression (web-submitted ACME 20260610): the address was configured
    as ``location.path: "20260610"`` on top of a connection whose
    ``host_path`` is the partner dir, so ``root_path`` already ends in the
    batch id (``/data/inboxarchive/ACME/20260610``) and the CSVs sit
    directly beneath it. The pipelined and flat layouts double-apply the
    batch (``.../20260610/acme/20260610/``), so resolution must fall back
    to the root-direct layout ``{root_path}/{file_pattern}``.
    """
    from ovid2.drivers.disc.spark import resolve_read_path

    # root_path already IS the batch dir; file sits directly inside it.
    target = tmp_path / "person.csv"
    target.write_text("a\n1\n", encoding="utf-8")

    resolved = resolve_read_path(str(tmp_path), "acme", "20260610", "*person.csv")
    assert resolved == str(target)


@pytest.mark.unit
def test_resolve_read_path_root_direct_never_shadows_pipelined(tmp_path: Path) -> None:
    """Root-direct is tried last: a correctly-scoped layout always wins."""
    from ovid2.drivers.disc.spark import resolve_read_path

    # A stray file directly under the root must NOT win over the proper
    # pipelined layout when the latter exists.
    (tmp_path / "person.csv").write_text("root\n", encoding="utf-8")
    proper = tmp_path / "acme" / "20260610" / "person.csv"
    proper.parent.mkdir(parents=True)
    proper.write_text("scoped\n", encoding="utf-8")

    resolved = resolve_read_path(str(tmp_path), "acme", "20260610", "person.csv")
    assert resolved == str(proper)


@pytest.mark.unit
def test_resolve_read_path_missing_names_root_direct_layout(tmp_path: Path) -> None:
    """The 'not found' error names all three attempted layouts."""
    from ovid2.drivers.disc.spark import resolve_read_path

    with pytest.raises(FileNotFoundError) as excinfo:
        resolve_read_path(str(tmp_path), "acme_from_disc", "20260610", "person.csv")

    message = str(excinfo.value)
    assert "flat layout" in message
    assert "root-direct layout" in message
