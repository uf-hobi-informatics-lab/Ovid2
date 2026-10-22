"""Unit tests for ``register_shared_tables`` shared-view registration.

Focus: the OMOP CONCEPT.csv column-case normalisation. The Athena OMOP
export ships UPPERCASE headers (``CONCEPT_ID``, ``DOMAIN_ID``,
``CONCEPT_CODE``, …), but the semantic-layer vocabularies and every
``semantic_join`` / ``omop_concept_join`` pipeline spec reference the
columns in lowercase. ``register_shared_tables`` must therefore expose
the ``concept`` temp view with lowercase column names, matching the
legacy ``commonFunctions.spark_read_omop_concept`` behaviour.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ovid2.cdm.handle import CdmHandle, SharedViewSpec
from ovid2.core.pipeline import register_shared_tables

if TYPE_CHECKING:
    from pyspark.sql import SparkSession


def _write_concept_csv(tmp_path: Path) -> Path:
    """Write a tab-delimited CONCEPT.csv with UPPERCASE Athena-style headers."""
    path = tmp_path / "CONCEPT.csv"
    path.write_text(
        "CONCEPT_ID\tCONCEPT_NAME\tDOMAIN_ID\tVOCABULARY_ID\tCONCEPT_CODE\n"
        "100\tHypertension\tCondition\tSNOMED\tI10\n"
        "200\tMale\tGender\tGender\tM\n",
        encoding="utf-8",
    )
    return path


def test_concept_view_columns_are_lowercased(
    spark: "SparkSession", tmp_path: Path
) -> None:
    """The registered ``concept`` view exposes lowercase column names."""
    concept_path = _write_concept_csv(tmp_path)
    cdm = CdmHandle(
        name="test_cdm",
        shared_views=(
            SharedViewSpec(
                view_name="concept",
                source_path=concept_path,
                delimiter="\t",
                header=True,
                required=True,
            ),
        ),
    )

    # Drop any leftover ``concept`` view from an earlier test in the same
    # Spark session — ``register_shared_tables`` skips already-registered
    # views, so a stale registration would mask this test's own output.
    if spark.catalog.tableExists("concept"):
        spark.catalog.dropTempView("concept")

    register_shared_tables(spark, cdm=cdm)

    view = spark.table("concept")
    assert view.columns == [
        "concept_id",
        "concept_name",
        "domain_id",
        "vocabulary_id",
        "concept_code",
    ]
    # A lowercase reference (as every pipeline spec uses) resolves.
    filtered = view.filter(view.domain_id == "Condition")
    rows = filtered.collect()
    assert len(rows) == 1
    assert rows[0]["concept_code"] == "I10"

    spark.catalog.dropTempView("concept")


def test_missing_optional_shared_view_is_skipped(
    spark: "SparkSession", tmp_path: Path
) -> None:
    """A missing, non-required shared view warns and is skipped (no raise)."""
    cdm = CdmHandle(
        name="test_cdm_missing",
        shared_views=(
            SharedViewSpec(
                view_name="concept_absent",
                source_path=tmp_path / "does_not_exist.csv",
                required=False,
            ),
        ),
    )

    # Should not raise.
    register_shared_tables(spark, cdm=cdm)
    assert not spark.catalog.tableExists("concept_absent")


def test_missing_required_shared_view_raises(
    spark: "SparkSession", tmp_path: Path
) -> None:
    """A missing, required shared view raises FileNotFoundError."""
    cdm = CdmHandle(
        name="test_cdm_required",
        shared_views=(
            SharedViewSpec(
                view_name="concept_required",
                source_path=tmp_path / "missing.csv",
                required=True,
            ),
        ),
    )

    with pytest.raises(FileNotFoundError):
        register_shared_tables(spark, cdm=cdm)


# ---------------------------------------------------------------------------
# Non-Spark (Snowpark) file-sourced shared view — routes through the stager
# ---------------------------------------------------------------------------


class _FakeDataFrame:
    """A DataFrame double with no ``spark.read``/``toDF`` — Snowpark-like."""

    def __init__(self, columns: list[str]) -> None:
        self.columns = columns
        self.registered_as: str | None = None

    # Snowpark spelling of ``createOrReplaceTempView``.
    def create_or_replace_temp_view(self, name: str) -> None:
        self.registered_as = name


class _FakeStager:
    """Captures the ``materialise`` call the file path should make."""

    def __init__(self, df: _FakeDataFrame) -> None:
        self._df = df
        self.calls: list[dict] = []

    def materialise(
        self, driver, pipeline, batch_id, file_name, delimiter=None, *, table_name=None
    ):
        self.calls.append(
            {
                "driver": driver,
                "pipeline": pipeline,
                "batch_id": batch_id,
                "file_name": file_name,
                "delimiter": delimiter,
                "table_name": table_name,
            }
        )
        return self._df


class _FakeSnowparkSession:
    """A session double that has no ``read`` attribute (like the real bug)."""


class _FakeEngine:
    """A non-Spark engine exposing ``session_is_spark = False``."""

    session_is_spark = False

    def __init__(self, stager: _FakeStager) -> None:
        self._stager = stager

    def get_stager(self) -> _FakeStager:
        return self._stager


def test_file_view_on_non_spark_engine_routes_through_stager(
    tmp_path: Path,
) -> None:
    """A file-sourced view on a non-Spark engine stages via a disc driver.

    Reproduces the original failure — ``'SnowparkEngineSession' object
    has no attribute 'read'`` — and asserts the fix: the file is
    materialised through the engine's stager (synthetic ``disc``
    driver rooted at the file's parent) and registered as a temp view,
    without ever touching ``session.read.csv``.
    """
    from ovid2.drivers.disc.driver import DiscDriver

    concept_path = _write_concept_csv(tmp_path)
    cdm = CdmHandle(
        name="test_cdm_snowpark",
        shared_views=(
            SharedViewSpec(
                view_name="concept",
                source_path=concept_path,
                delimiter="\t",
                header=True,
                required=True,
            ),
        ),
    )

    df = _FakeDataFrame(["CONCEPT_ID", "CONCEPT_CODE", "DOMAIN_ID"])
    stager = _FakeStager(df)
    engine = _FakeEngine(stager)
    session = _FakeSnowparkSession()

    # Must not raise AttributeError('read') — the whole point of the fix.
    register_shared_tables(
        session, cdm=cdm, engine=engine, pipeline="pcornet_to_omop", batch_id="PUBLIC",
    )

    # Registered via the Snowpark spelling.
    assert df.registered_as == "concept"

    # The stager was called exactly once with a synthetic disc driver
    # rooted at the file's parent dir and the file basename.
    assert len(stager.calls) == 1
    call = stager.calls[0]
    driver = call["driver"]
    assert isinstance(driver, DiscDriver)
    merged = driver._merged_location_view()  # noqa: SLF001
    assert merged["root"] == str(concept_path.parent)
    assert call["file_name"] == "CONCEPT.csv"
    assert call["table_name"] == "concept"
    # Empty pipeline/batch so resolve_read_path lands on the exact file.
    assert call["pipeline"] == ""
    assert call["batch_id"] == ""


def test_file_view_non_spark_missing_optional_is_skipped(
    tmp_path: Path,
) -> None:
    """A missing optional file view on a non-Spark engine warns, no stager call."""
    cdm = CdmHandle(
        name="test_cdm_snowpark_missing",
        shared_views=(
            SharedViewSpec(
                view_name="concept_absent",
                source_path=tmp_path / "does_not_exist.csv",
                required=False,
            ),
        ),
    )
    stager = _FakeStager(_FakeDataFrame([]))
    engine = _FakeEngine(stager)

    register_shared_tables(_FakeSnowparkSession(), cdm=cdm, engine=engine)

    # Missing-file check happens before the stager is consulted.
    assert stager.calls == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))