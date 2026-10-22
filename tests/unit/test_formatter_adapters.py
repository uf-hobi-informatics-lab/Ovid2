"""Unit tests for the ``pcornet_passthrough`` and ``omop_concept_join`` adapters.

Uses the shared ``spark`` fixture; auto-skipped when no JVM is available.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

pytest.importorskip("pyspark")

from ovid2.formatters.adapters import (
    AdapterContext,
    PcornetPassthroughAdapter,
)
from ovid2.formatters.spec import load_from_dict, spec

if TYPE_CHECKING:
    from pyspark.sql import SparkSession


class _StubAddress:
    """Minimal input address that maps glob → predefined DataFrame."""

    def __init__(self, mapping: dict[str, object]) -> None:
        self._mapping = mapping

    def read(self, spark, pipeline, batch_id, file_name, delimiter=None, *, table_name=None):
        try:
            return self._mapping[file_name]
        except KeyError as exc:
            raise FileNotFoundError(file_name) from exc

    def write(self, *args, **kwargs) -> None:
        raise NotImplementedError


@pytest.mark.unit
def test_pcornet_passthrough_single_source(spark: SparkSession) -> None:
    """One source flows straight through the adapter."""
    df = spark.createDataFrame([("P1", "F"), ("P2", "M")], ["PATID", "sex"])
    address = _StubAddress({"*Demographic*": df})
    raw = spec(
        formatter="demographic",
        adapter="pcornet_passthrough",
        sources={"demographic": {"glob": "*Demographic*"}},
        projections={"PATID": "patid", "SEX": "sex"},
    )
    parsed = load_from_dict(raw, pipeline="testpipeline")

    ctx = AdapterContext(
        spark=spark, input_address=address, pipeline="testpipeline", batch_id="20260101"
    )
    result = PcornetPassthroughAdapter().resolve(parsed, ctx).collect()
    rows = sorted((row["patid"], row["sex"]) for row in result)
    assert rows == [("P1", "F"), ("P2", "M")]


@pytest.mark.unit
def test_pcornet_passthrough_filter(spark: SparkSession) -> None:
    """The source-level ``filter:`` is applied before joins/projections."""
    df = spark.createDataFrame([("P1", "F"), ("P2", "M"), ("P3", "F")], ["PATID", "sex"])
    address = _StubAddress({"*Demographic*": df})
    raw = spec(
        formatter="demographic",
        adapter="pcornet_passthrough",
        sources={
            "demographic": {"glob": "*Demographic*", "filter": "sex = 'F'"},
        },
        projections={"PATID": "patid"},
    )
    parsed = load_from_dict(raw, pipeline="testpipeline")

    ctx = AdapterContext(
        spark=spark, input_address=address, pipeline="testpipeline", batch_id="20260101"
    )
    result = PcornetPassthroughAdapter().resolve(parsed, ctx).collect()
    assert sorted(r["patid"] for r in result) == ["P1", "P3"]


@pytest.mark.unit
def test_omop_concept_join_prefixes_prevent_ambiguity(spark: SparkSession) -> None:
    """Joining ``concept`` twice with different prefixes remains unambiguous."""
    # Source: one condition_occurrence row with two concept_id columns.
    condition = spark.createDataFrame(
        [("P1", 100, 200)],
        ["person_id", "condition_concept_id", "condition_type_concept_id"],
    )
    concept = spark.createDataFrame(
        [
            (100, "I10", "SNOMED", "Hypertension", "Condition"),
            (200, "EHR", "TypeConc", "EHR problem list", "Type Concept"),
        ],
        ["concept_id", "concept_code", "vocabulary_id", "concept_name", "domain_id"],
    )
    concept.createOrReplaceTempView("concept")

    address = _StubAddress({"*condition_occurrence*": condition})

    raw = spec(
        formatter="condition",
        adapter="omop_concept_join",
        sources={
            "condition_occurrence": {"glob": "*condition_occurrence*"},
        },
        joins=[
            {
                "with": "concept",
                "on": "concept.concept_id = condition_occurrence.condition_concept_id",
                "how": "left",
                "prefix": "cond",
                "domain_filter": ["Condition"],
            },
            {
                "with": "concept",
                "on": "concept.concept_id = condition_occurrence.condition_type_concept_id",
                "how": "left",
                "prefix": "cond_type",
                "domain_filter": ["Type Concept"],
            },
        ],
        projections={
            "PATID": "condition_occurrence.person_id",
            "CONDITION": "cond_concept_code",
            "CONDITION_TYPE": "cond_vocabulary_id",
            "RAW_CONDITION_SOURCE": "cond_type_concept_name",
        },
    )
    parsed = load_from_dict(raw, pipeline="testpipeline")

    from ovid2.formatters.engine import execute_spec

    ctx = AdapterContext(
        spark=spark, input_address=address, pipeline="testpipeline", batch_id="20260101"
    )
    result = execute_spec(parsed, ctx).collect()
    assert len(result) == 1
    row = result[0].asDict()
    assert row["PATID"] == "P1"
    assert row["CONDITION"] == "I10"
    assert row["CONDITION_TYPE"] == "SNOMED"
    assert row["RAW_CONDITION_SOURCE"] == "EHR problem list"


@pytest.mark.unit
def test_omop_concept_join_domain_filter(spark: SparkSession) -> None:
    """``domain_filter`` restricts the joined concept view."""
    # A concept with id=100 exists in both Condition and Drug domains — the
    # domain_filter must pick only the Condition row.
    condition = spark.createDataFrame([("P1", 100)], ["person_id", "condition_concept_id"])
    concept = spark.createDataFrame(
        [
            (100, "I10", "SNOMED", "Hypertension", "Condition"),
            (100, "DRUG", "RXNORM", "Some drug", "Drug"),
        ],
        ["concept_id", "concept_code", "vocabulary_id", "concept_name", "domain_id"],
    )
    concept.createOrReplaceTempView("concept")

    address = _StubAddress({"*condition_occurrence*": condition})

    raw = spec(
        formatter="condition",
        adapter="omop_concept_join",
        sources={"condition_occurrence": {"glob": "*condition_occurrence*"}},
        joins=[
            {
                "with": "concept",
                "on": "concept.concept_id = condition_occurrence.condition_concept_id",
                "how": "left",
                "prefix": "cond",
                "domain_filter": ["Condition"],
            },
        ],
        projections={"PATID": "condition_occurrence.person_id", "CONDITION": "cond_concept_code"},
    )
    parsed = load_from_dict(raw, pipeline="testpipeline")

    from ovid2.formatters.engine import execute_spec

    ctx = AdapterContext(
        spark=spark, input_address=address, pipeline="testpipeline", batch_id="20260101"
    )
    result = execute_spec(parsed, ctx).collect()
    codes = [row["CONDITION"] for row in result]
    assert codes == ["I10"]  # DRUG filtered out
