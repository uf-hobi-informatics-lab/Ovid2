"""Unit tests for the ``semantic_join`` formatter adapter (S2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

pytest.importorskip("pyspark")

from ovid2.formatters.adapters import AdapterContext, OmopConceptJoinAdapter, SemanticJoinAdapter
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
def test_semantic_join_via_vocabulary_name(spark: "SparkSession") -> None:
    """``with: omop_concept`` resolves through the semantic registry."""
    # OMOP CONCEPT is a bundled vocabulary; its temp view is ``concept``.
    concept = spark.createDataFrame(
        [(100, "I10", "SNOMED", "Hypertension", "Condition")],
        ["concept_id", "concept_code", "vocabulary_id", "concept_name", "domain_id"],
    )
    concept.createOrReplaceTempView("concept")

    condition = spark.createDataFrame([("P1", 100)], ["person_id", "condition_concept_id"])
    address = _StubAddress({"*condition_occurrence*": condition})

    raw = spec(
        formatter="condition",
        adapter="semantic_join",
        sources={"condition_occurrence": {"glob": "*condition_occurrence*"}},
        joins=[
            {
                "with": "omop_concept",
                "on": "omop_concept.concept_id = condition_occurrence.condition_concept_id",
                "how": "left",
                "prefix": "cond",
                "domain_filter": ["Condition"],
            },
        ],
        projections={
            "PATID": "condition_occurrence.person_id",
            "CONDITION": "cond_concept_code",
        },
    )
    parsed = load_from_dict(raw, pipeline="testpipeline")

    ctx = AdapterContext(
        spark=spark, input_address=address, pipeline="testpipeline", batch_id="20260101"
    )
    result = SemanticJoinAdapter().resolve(parsed, ctx)
    rows = result.collect()
    # The right-side is prefixed with `cond_`; every column is present.
    assert "cond_concept_code" in result.columns
    assert "cond_domain_id" in result.columns
    assert len(rows) == 1


@pytest.mark.unit
def test_semantic_join_falls_back_to_temp_view(spark: "SparkSession") -> None:
    """Unknown ``with:`` names fall back to ``spark.table(name)``.

    This is how OMOP-shaped specs that name the raw ``concept`` view
    (rather than the ``omop_concept`` vocabulary) continue to work.
    """
    concept = spark.createDataFrame(
        [(100, "I10", "SNOMED", "Hypertension", "Condition")],
        ["concept_id", "concept_code", "vocabulary_id", "concept_name", "domain_id"],
    )
    concept.createOrReplaceTempView("concept")

    condition = spark.createDataFrame([("P1", 100)], ["person_id", "condition_concept_id"])
    address = _StubAddress({"*condition_occurrence*": condition})

    raw = spec(
        formatter="condition",
        adapter="semantic_join",
        sources={"condition_occurrence": {"glob": "*condition_occurrence*"}},
        joins=[
            {
                "with": "concept",  # not a registered vocabulary — falls back
                "on": "concept.concept_id = condition_occurrence.condition_concept_id",
                "how": "left",
                "prefix": "cond",
                "domain_filter": ["Condition"],
            },
        ],
        projections={
            "PATID": "condition_occurrence.person_id",
            "CONDITION": "cond_concept_code",
        },
    )
    parsed = load_from_dict(raw, pipeline="testpipeline")

    ctx = AdapterContext(
        spark=spark, input_address=address, pipeline="testpipeline", batch_id="20260101"
    )
    result = SemanticJoinAdapter().resolve(parsed, ctx).collect()
    assert len(result) == 1


@pytest.mark.unit
def test_omop_concept_join_shim_still_works(spark: "SparkSession") -> None:
    """The legacy ``omop_concept_join`` adapter is a subclass of the new one.

    Existing YAML specs continue to load through the same class name.
    """
    concept = spark.createDataFrame(
        [(100, "I10", "SNOMED", "Hypertension", "Condition")],
        ["concept_id", "concept_code", "vocabulary_id", "concept_name", "domain_id"],
    )
    concept.createOrReplaceTempView("concept")

    condition = spark.createDataFrame([("P1", 100)], ["person_id", "condition_concept_id"])
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
        projections={
            "PATID": "condition_occurrence.person_id",
            "CONDITION": "cond_concept_code",
        },
    )
    parsed = load_from_dict(raw, pipeline="testpipeline")

    ctx = AdapterContext(
        spark=spark, input_address=address, pipeline="testpipeline", batch_id="20260101"
    )
    result = OmopConceptJoinAdapter().resolve(parsed, ctx).collect()
    assert len(result) == 1


@pytest.mark.unit
def test_omop_concept_join_is_subclass_of_semantic_join() -> None:
    """The compatibility shim inherits — behaviour is identical."""
    assert issubclass(OmopConceptJoinAdapter, SemanticJoinAdapter)


@pytest.mark.unit
def test_semantic_join_domain_filter_via_vocabulary(spark: "SparkSession") -> None:
    """domain_filter uses the vocabulary's ``domain_column``."""
    concept = spark.createDataFrame(
        [
            (100, "I10", "SNOMED", "Hypertension", "Condition"),
            (100, "DRUG", "RXNORM", "Some drug", "Drug"),
        ],
        ["concept_id", "concept_code", "vocabulary_id", "concept_name", "domain_id"],
    )
    concept.createOrReplaceTempView("concept")

    condition = spark.createDataFrame([("P1", 100)], ["person_id", "condition_concept_id"])
    address = _StubAddress({"*condition_occurrence*": condition})

    raw = spec(
        formatter="condition",
        adapter="semantic_join",
        sources={"condition_occurrence": {"glob": "*condition_occurrence*"}},
        joins=[
            {
                "with": "omop_concept",
                "on": "omop_concept.concept_id = condition_occurrence.condition_concept_id",
                "how": "left",
                "prefix": "cond",
                "domain_filter": ["Condition"],
            },
        ],
        projections={
            "PATID": "condition_occurrence.person_id",
            "CONDITION": "cond_concept_code",
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
    assert row["CONDITION"] == "I10"
