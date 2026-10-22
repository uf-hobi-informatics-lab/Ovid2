"""Unit tests for the OMOP CDM 5.3 handle (``omop_5_3``).

These lock in the shape produced by ``scripts/gen_omop_cdm_yaml.py`` from
the legacy converter's authoritative ``SF_SCHEMAS`` definitions, so a
regeneration or hand-edit that drops a core table / column / key is
caught immediately.
"""

from __future__ import annotations

import pytest

# Import the package for its self-registration side effect.
import ovid2.cdm  # noqa: F401
from ovid2.cdm.registry import get_cdm, registered_cdms
from ovid2.schema.cdm import build_snowflake_types_ddl

_CORE_CLINICAL_TABLES = {
    "person",
    "visit_occurrence",
    "condition_occurrence",
    "drug_exposure",
    "procedure_occurrence",
    "device_exposure",
    "measurement",
    "observation",
    "death",
}


def test_omop_5_3_is_registered() -> None:
    assert "omop_5_3" in registered_cdms()


def test_omop_5_3_metadata() -> None:
    handle = get_cdm("omop_5_3")
    assert handle.name == "omop_5_3"
    assert handle.version == "5.3"


def test_omop_5_3_carries_core_clinical_tables() -> None:
    handle = get_cdm("omop_5_3")
    tables = set(handle.tables())
    missing = _CORE_CLINICAL_TABLES - tables
    assert not missing, f"missing core OMOP tables: {sorted(missing)}"


def test_reference_tables_are_ordered_first() -> None:
    handle = get_cdm("omop_5_3")
    order = handle.tables()
    # person / provider / care_site / location must precede any table
    # that references them (e.g. visit_occurrence, condition_occurrence).
    assert order.index("person") < order.index("visit_occurrence")
    assert order.index("location") < order.index("person")
    assert order.index("care_site") < order.index("person")
    for ref in ("location", "care_site", "provider", "person", "visit_occurrence"):
        assert ref in handle.reference_tables


@pytest.mark.parametrize(
    ("table", "pk"),
    [
        ("person", ("person_id",)),
        ("visit_occurrence", ("visit_occurrence_id",)),
        ("condition_occurrence", ("condition_occurrence_id",)),
        ("measurement", ("measurement_id",)),
        ("drug_exposure", ("drug_exposure_id",)),
        ("observation", ("observation_id",)),
        ("procedure_occurrence", ("procedure_occurrence_id",)),
        ("death", ("person_id",)),
    ],
)
def test_composite_keys(table: str, pk: tuple[str, ...]) -> None:
    handle = get_cdm("omop_5_3")
    assert handle.dedup_key_for(table) == pk


def test_id_columns_present() -> None:
    handle = get_cdm("omop_5_3")
    assert handle.id_column_for("person").column == "person_id"
    assert handle.id_column_for("visit_occurrence").column == "visit_occurrence_id"


def test_shared_views_reuse_concept() -> None:
    handle = get_cdm("omop_5_3")
    names = {v.view_name for v in handle.shared_views}
    assert {"concept", "concept_relationship"} <= names


def test_person_fields_and_types() -> None:
    handle = get_cdm("omop_5_3")
    fields = {f.name: f.rdbms_type for f in handle.load_table_fields("person")}
    # A representative typed column set.
    assert fields["PERSON_ID"] == "RDBMS Number"
    assert fields["GENDER_CONCEPT_ID"] == "RDBMS Number"
    assert fields["PERSON_SOURCE_VALUE"].startswith("RDBMS Text(")
    # Ovid trailing bookkeeping columns are preserved.
    assert "UPDATED" in fields
    assert "SOURCE" in fields
    assert "MAPPED_FROM" in fields


def test_snowflake_ddl_generation() -> None:
    ddl = build_snowflake_types_ddl("omop_5_3.PERSON")
    assert ddl.startswith("PERSON_ID NUMBER(38,10)")
    # person_source_value carries PATID verbatim; on tokenized network
    # feeds this is a variable-length Datavant token, so the CDM YAML
    # widens it to Snowflake's max VARCHAR (see CHANGELOG — width overflow
    # fix). A fixed intermediate width would eventually overflow.
    assert "PERSON_SOURCE_VALUE VARCHAR(16777216)" in ddl
    # condition_occurrence carries typed DATE columns.
    cddl = build_snowflake_types_ddl("omop_5_3.CONDITION_OCCURRENCE")
    assert "CONDITION_START_DATE DATE" in cddl
