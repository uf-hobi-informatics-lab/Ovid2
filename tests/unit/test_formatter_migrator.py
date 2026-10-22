"""Unit tests for the legacy-formatter → YAML-spec migrator."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from ovid2.tools.formatter_migrator import (
    MigrationError,
    analyse,
    migrate_file,
    render_yaml_spec,
)

SIMPLE_SOURCE = dedent(
    """
    from pyspark.sql import DataFrame
    from pyspark.sql.functions import lit
    from transformations.formatters.base import BaseFormatter

    INPUT_FILE_NAME = "*Condition_*.txt"

    class ConditionFormatter(BaseFormatter):
        @property
        def table_name(self) -> str:
            return "condition"

        @property
        def input_file_name(self) -> str:
            return INPUT_FILE_NAME

        def format(self, df: DataFrame) -> DataFrame:
            return df.select(
                df['PATID'].alias('PATID'),
                df['CONDITION'].alias('CONDITION'),
                lit('').alias('RAW_CONDITION'),
                lit(None).alias('CONDITION_SOURCE'),
            )
    """
).strip()


COMPLEX_SOURCE = dedent(
    """
    from pyspark.sql import DataFrame
    from transformations.formatters.base import BaseFormatter

    INPUT_FILE_NAME = "*.txt"

    class ObsClinFormatter(BaseFormatter):
        @property
        def table_name(self) -> str:
            return "obs_clin"

        @property
        def input_file_name(self) -> str:
            return INPUT_FILE_NAME

        def format(self, df: DataFrame) -> DataFrame:
            df = df.withColumn('X', df['Y'])
            return df.select(df['X'].alias('X'))
    """
).strip()


@pytest.mark.unit
def test_analyse_extracts_glob_table_and_projections() -> None:
    """A minimal passthrough formatter maps to a full :class:`FormatterAnalysis`."""
    result = analyse(SIMPLE_SOURCE, pipeline="test", module_path="condition_formatter.py")
    assert result.table == "condition"
    assert result.input_glob == "*Condition_*.txt"
    assert result.class_name == "ConditionFormatter"

    assert result.projections["PATID"] == "patid"
    assert result.projections["CONDITION"] == "condition"
    assert result.projections["RAW_CONDITION"] == "''"
    assert result.projections["CONDITION_SOURCE"] == "CAST(NULL AS STRING)"


@pytest.mark.unit
def test_analyse_rejects_complex_formatter() -> None:
    """Formatters that call ``withColumn`` etc. cannot be auto-migrated."""
    with pytest.raises(MigrationError, match="withColumn"):
        analyse(COMPLEX_SOURCE, pipeline="test", module_path="obs_clin_formatter.py")


@pytest.mark.unit
def test_render_yaml_spec_produces_valid_yaml() -> None:
    """The rendered YAML must parse and contain the expected top-level keys."""
    analysis = analyse(SIMPLE_SOURCE, pipeline="test", module_path="condition_formatter.py")
    rendered = render_yaml_spec(analysis)
    loaded = yaml.safe_load(rendered)
    assert loaded["v"] == 1
    assert loaded["formatter"] == "condition"
    assert loaded["adapter"] == "pcornet_passthrough"
    assert loaded["input_file_name"] == "*Condition_*.txt"
    assert loaded["sources"]["condition"]["glob"] == "*Condition_*.txt"
    assert loaded["projections"]["PATID"] == "patid"


@pytest.mark.unit
def test_migrate_file_writes_to_destination(tmp_path: Path) -> None:
    """`migrate_file` should write a ``<table>.yaml`` file into the destination."""
    source_path = tmp_path / "condition_formatter.py"
    source_path.write_text(SIMPLE_SOURCE, encoding="utf-8")

    target, analysis = migrate_file(
        source_path, pipeline="test", destination_dir=tmp_path / "out"
    )
    assert target.name == "condition.yaml"
    assert target.is_file()
    assert analysis.table == "condition"

    # Re-running without overwrite must refuse to clobber.
    with pytest.raises(FileExistsError):
        migrate_file(source_path, pipeline="test", destination_dir=tmp_path / "out")

    # ...but --overwrite succeeds.
    migrate_file(
        source_path, pipeline="test", destination_dir=tmp_path / "out", overwrite=True
    )


@pytest.mark.unit
def test_migrated_spec_passes_ovid2_schema(tmp_path: Path) -> None:
    """The generated YAML must validate against the formatter-spec schema."""
    from ovid2.formatters.spec import load_from_yaml

    source_path = tmp_path / "condition_formatter.py"
    source_path.write_text(SIMPLE_SOURCE, encoding="utf-8")
    target, _ = migrate_file(source_path, pipeline="test", destination_dir=tmp_path)
    spec = load_from_yaml(target, pipeline="test")
    assert spec.formatter == "condition"
    assert spec.adapter == "pcornet_passthrough"
