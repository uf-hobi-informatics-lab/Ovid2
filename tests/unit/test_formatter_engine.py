"""Unit tests for the formatter engine's projection/validation logic."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ovid2.formatters.engine import _projection_expr

pytest.importorskip("pyspark")

from ovid2.formatters.adapters import AdapterContext
from ovid2.formatters.engine import FormatterSpecError, execute_spec
from ovid2.formatters.spec import load_from_dict, spec

if TYPE_CHECKING:
    from pyspark.sql import SparkSession


class _StubAddress:
    def __init__(self, mapping: dict[str, object]) -> None:
        self._mapping = mapping

    def read(self, spark, pipeline, batch_id, file_name, delimiter=None, *, table_name=None):
        return self._mapping[file_name]

    def write(self, *args, **kwargs) -> None:
        raise NotImplementedError


@pytest.mark.unit
def test_spec_formatter_input_file_name_from_custom_class() -> None:
    """A ``custom`` spec surfaces its class's ``input_file_name``.

    Regression: ``pcornet_to_omop`` YAMLs use ``adapter: custom`` with no
    ``input_file_name`` field, so ``_SpecFormatter.input_file_name`` fell
    through to ``"*"``. The pipeline's placeholder read then used the CDM
    *output* table name (e.g. ``person``) as the Snowflake ``dbtable``
    fallback and crashed with ``Object 'PERSON' does not exist`` — even
    though the real source is PCORnet ``DEMOGRAPHIC``. The property now
    consults the hand-written class so the placeholder read targets the
    true source identifier.
    """
    from ovid2.formatters.engine import _SpecFormatter
    from ovid2.formatters.spec import find_and_load

    parsed = find_and_load("pcornet_to_omop", "person")
    formatter = _SpecFormatter(parsed, ctx=None)  # type: ignore[arg-type]
    assert formatter.input_file_name == "DEMOGRAPHIC"


@pytest.mark.unit
def test_spec_formatter_input_file_name_explicit_wins() -> None:
    """An explicit ``input_file_name`` on the spec beats the custom class."""
    from ovid2.formatters.engine import _SpecFormatter

    parsed = load_from_dict(
        {
            "v": 1,
            "formatter": "person",
            "adapter": "custom",
            "input_file_name": "OVERRIDE_TABLE",
            "class": "ovid2_pipelines.pcornet_to_omop.formatters_custom."
            "person_formatter.PersonFormatter",
        },
        pipeline="pcornet_to_omop",
    )
    formatter = _SpecFormatter(parsed, ctx=None)  # type: ignore[arg-type]
    assert formatter.input_file_name == "OVERRIDE_TABLE"


@pytest.mark.unit
def test_spec_formatter_input_file_name_falls_back_when_class_unresolvable() -> None:
    """An unresolvable ``custom`` class falls back to the source glob / ``"*"``.

    ``format()`` will surface the real import error later; the property must
    not raise.
    """
    from ovid2.formatters.engine import _SpecFormatter

    parsed = load_from_dict(
        {
            "v": 1,
            "formatter": "person",
            "adapter": "custom",
            "class": "ovid2_pipelines.pcornet_to_omop.nonexistent.Missing",
        },
        pipeline="pcornet_to_omop",
    )
    formatter = _SpecFormatter(parsed, ctx=None)  # type: ignore[arg-type]
    assert formatter.input_file_name == "*"


@pytest.mark.unit
def test_spec_formatter_primary_source_optional_true() -> None:
    """A spec whose primary source is ``optional: true`` reports it.

    Regression: ACME ``pcornet_trial`` declares ``optional: true`` on its
    only source (``*PREVENTABLE*``), but the pipeline's eager placeholder
    read raised ``FileNotFoundError`` before the adapter's optional-aware
    read ran. The shim now surfaces the flag so the pipeline can degrade
    to an empty table instead of failing the run.
    """
    from ovid2.formatters.engine import _SpecFormatter

    parsed = load_from_dict(
        {
            "v": 1,
            "formatter": "pcornet_trial",
            "adapter": "source_passthrough",
            "sources": {
                "pcornet_trial": {"glob": "*PREVENTABLE*", "optional": True},
            },
            "projections": {"PATID": "source_patid"},
        },
        pipeline="acme_from_disc",
    )
    formatter = _SpecFormatter(parsed, ctx=None)  # type: ignore[arg-type]
    assert formatter.primary_source_optional is True


@pytest.mark.unit
def test_spec_formatter_primary_source_optional_defaults_false() -> None:
    """A required primary source (or no sources) reports ``False``."""
    from ovid2.formatters.engine import _SpecFormatter

    parsed = load_from_dict(
        {
            "v": 1,
            "formatter": "demographic",
            "adapter": "source_passthrough",
            "sources": {"demographic": {"glob": "*demographic*"}},
            "projections": {"PATID": "patid"},
        },
        pipeline="acme_from_disc",
    )
    formatter = _SpecFormatter(parsed, ctx=None)  # type: ignore[arg-type]
    assert formatter.primary_source_optional is False


@pytest.mark.unit
def test_projection_expr_empty_string_yields_sql_empty_literal() -> None:
    """``COL: ''`` in YAML must render as the SQL empty-string literal, not
    a bareword.

    Regression: before this fix, an empty projection value produced
    ``" as COL"`` which Spark parsed as ``<column 'as'> AS COL`` and
    raised ``AnalysisException: Column 'as' does not exist``.
    """
    assert _projection_expr("") == "''"
    assert _projection_expr("   ") == "''"


@pytest.mark.unit
def test_projection_expr_passes_through_non_empty() -> None:
    """Non-empty expressions round-trip verbatim."""
    assert _projection_expr("patid") == "patid"
    assert _projection_expr("upper(sex)") == "upper(sex)"
    assert _projection_expr("CAST(NULL AS STRING)") == "CAST(NULL AS STRING)"
    assert _projection_expr("'literal'") == "'literal'"


@pytest.mark.unit
def test_execute_spec_applies_projections(spark: SparkSession) -> None:
    """The ``projections:`` block becomes a ``selectExpr``."""
    src = spark.createDataFrame([("p1", "F", "2020-01-01")], ["patid", "sex", "birth_date"])
    address = _StubAddress({"*Demographic*": src})
    raw = spec(
        formatter="demographic",
        adapter="pcornet_passthrough",
        sources={"demographic": {"glob": "*Demographic*"}},
        projections={
            "PATID": "upper(patid)",
            "SEX": "sex",
        },
    )
    parsed = load_from_dict(raw, pipeline="testpipeline")
    ctx = AdapterContext(
        spark=spark, input_address=address, pipeline="testpipeline", batch_id="20260101"
    )
    df = execute_spec(parsed, ctx)
    assert df.columns == ["PATID", "SEX"]
    row = df.collect()[0]
    assert row["PATID"] == "P1"
    assert row["SEX"] == "F"


@pytest.mark.unit
def test_execute_spec_missing_optional_source_yields_empty_projected_df(
    spark: SparkSession,
) -> None:
    """A missing *optional* source yields an empty, correctly-shaped output.

    Regression: ACME ``pcornet_trial`` (``source_passthrough``, single
    ``optional: true`` source ``*PREVENTABLE*``) crashed on a batch with
    no trial extract. ``read_source`` returned a column-less empty
    DataFrame, and the ``projections:`` block then failed with
    ``Column 'source_patid' does not exist``. ``execute_spec`` now
    detects the column-less source and synthesises an empty output whose
    columns are the projection keys, so the table lands empty instead of
    failing the run.
    """

    class _MissingAddress:
        def read(self, *a: object, **k: object) -> object:
            raise FileNotFoundError("no *PREVENTABLE* files")

        def write(self, *a: object, **k: object) -> None:
            raise NotImplementedError

    raw = spec(
        formatter="pcornet_trial",
        adapter="source_passthrough",
        sources={"pcornet_trial": {"glob": "*PREVENTABLE*", "optional": True}},
        projections={
            "PATID": "source_patid",
            "TRIALID": "trialid",
            "TRIAL_INVITE_CODE": "''",
            "SEX": "sex",
        },
    )
    parsed = load_from_dict(raw, pipeline="testpipeline")
    ctx = AdapterContext(
        spark=spark,
        input_address=_MissingAddress(),
        pipeline="testpipeline",
        batch_id="20260610",
    )
    df = execute_spec(parsed, ctx)
    assert df.columns == ["PATID", "TRIALID", "TRIAL_INVITE_CODE", "SEX"]
    assert df.count() == 0


@pytest.mark.unit
def test_execute_spec_rejects_disabled_spec(spark: SparkSession) -> None:
    """Disabled specs cannot be executed."""
    raw = spec(
        formatter="demographic",
        adapter="pcornet_passthrough",
        enabled=False,
        sources={"x": {"glob": "*"}},
        projections={"PATID": "patid"},
    )
    parsed = load_from_dict(raw, pipeline="testpipeline")
    ctx = AdapterContext(
        spark=spark, input_address=_StubAddress({}), pipeline="testpipeline", batch_id="20260101"
    )
    with pytest.raises(FormatterSpecError, match="disabled"):
        execute_spec(parsed, ctx)


@pytest.mark.unit
def test_execute_spec_validates_output_columns_when_cdm_present(
    spark: SparkSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the CDM schema is available, missing columns fail loudly."""
    from ovid2.schema import cdm

    # Fake CDM schema: demographic requires PATID and BIRTH_DATE.
    monkeypatch.setattr(
        cdm,
        "load_cdm_table_columns",
        lambda ref: ["PATID", "BIRTH_DATE"] if ref == "pcornet_7_0.DEMOGRAPHIC" else [],
    )

    src = spark.createDataFrame([("p1",)], ["patid"])
    address = _StubAddress({"*Demographic*": src})
    raw = spec(
        formatter="demographic",
        adapter="pcornet_passthrough",
        sources={"demographic": {"glob": "*Demographic*"}},
        projections={"PATID": "patid"},  # missing BIRTH_DATE
        output_columns_from="pcornet_7_0.DEMOGRAPHIC",
    )
    parsed = load_from_dict(raw, pipeline="testpipeline")
    ctx = AdapterContext(
        spark=spark, input_address=address, pipeline="testpipeline", batch_id="20260101"
    )
    with pytest.raises(FormatterSpecError, match="missing CDM columns"):
        execute_spec(parsed, ctx)


@pytest.mark.unit
def test_execute_spec_empty_projection_fills_empty_string(spark: SparkSession) -> None:
    """A spec projection with an empty value must produce ``''`` cells.

    End-to-end guard against the ``Column 'as' does not exist`` regression:
    ``ENCOUNTERID: ''`` used to render as ``" as ENCOUNTERID"`` in
    ``selectExpr`` and crash. The engine now emits the SQL empty-string
    literal instead, matching the legacy ``lit('').alias(...)`` convention
    the ``formatter_migrator`` was built to preserve.
    """
    src = spark.createDataFrame([("p1", "some_med")], ["patid", "raw_ext_med_name"])
    address = _StubAddress({"*external_meds.csv": src})
    raw = spec(
        formatter="external_meds",
        adapter="pcornet_passthrough",
        sources={"external_meds": {"glob": "*external_meds.csv", "lowercase_columns": True}},
        projections={
            "PATID": "patid",
            "ENCOUNTERID": "",  # legacy lit('') convention
            "RAW_EXT_MED_NAME": "raw_ext_med_name",
        },
    )
    parsed = load_from_dict(raw, pipeline="testpipeline")
    ctx = AdapterContext(
        spark=spark, input_address=address, pipeline="testpipeline", batch_id="20260101"
    )
    df = execute_spec(parsed, ctx)
    assert df.columns == ["PATID", "ENCOUNTERID", "RAW_EXT_MED_NAME"]
    row = df.collect()[0]
    assert row["PATID"] == "p1"
    assert row["ENCOUNTERID"] == ""
    assert row["RAW_EXT_MED_NAME"] == "some_med"
