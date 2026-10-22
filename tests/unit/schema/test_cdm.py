"""Unit tests for :mod:`ovid2.schema.cdm`.

Focused on the Snowflake DDL builder introduced with the
``SnowflakeAddress.write`` minimal port. The tests build a small
in-memory CSV, point the module at it via ``monkeypatch``, and assert
the DDL fragment matches the expected type mapping.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ovid2.schema import cdm as cdm_module
from ovid2.schema.cdm import (
    CdmField,
    _snowflake_type_for,
    build_snowflake_types_ddl,
    clear_cache,
    load_cdm_full,
    load_cdm_table_columns,
)

_CDM_CSV = (
    "TABLE_NAME,FIELD_NAME,RDBMS_DATA_TYPE,SAS_DATA_TYPE\n"
    "HASH_TOKEN,PATID,RDBMS Text(x),SAS Char(x)\n"
    "HASH_TOKEN,TOKEN_ENCRYPTION_KEY_ID,RDBMS Text(50),SAS Char(50)\n"
    "HASH_TOKEN,TOKEN_01,RDBMS Text(256),SAS Char(256)\n"
    "DEMOGRAPHIC,PATID,RDBMS Text(x),SAS Char(x)\n"
    "DEMOGRAPHIC,BIRTH_DATE,RDBMS Date,SAS Date (Numeric)\n"
    "DEMOGRAPHIC,SEX,RDBMS Text(2),SAS Char(2)\n"
    "LAB_RESULT_CM,LAB_RESULT_CM_ID,RDBMS Text(x),SAS Char(x)\n"
    "LAB_RESULT_CM,ENCOUNTERID,RDBMS Text(x),SAS Char(x)\n"
    "LAB_RESULT_CM,RESULT_QUAL,RDBMS Text(20),SAS Char(20)\n"
    "LAB_RESULT_CM,RESULT_NUM,RDBMS Number(x),SAS Numeric\n"
    "ODDBALL,MYSTERY,SomethingUnknown,SAS Char(1)\n"
)


@pytest.fixture(autouse=True)
def _isolated_cdm_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the module at a synthetic CSV per test and clear the cache.

    Also disables the YAML branch (phase C1) that would otherwise
    short-circuit ``_load_all`` and return the real, packaged PCORnet
    schema instead of the tiny synthetic fixture below.
    """
    csv_path = tmp_path / "pcornet_cdm_v70.csv"
    csv_path.write_text(_CDM_CSV, encoding="utf-8")
    # ``_cdm_source_path`` consults the CDM registry first (P10) and only
    # falls back to ``PCORNET_CDM_CSV_PATH``, so a direct override of the
    # resolver is the only reliable way to point the module at the
    # synthetic CSV independent of the registry state.
    monkeypatch.setattr(cdm_module, "_cdm_source_path", lambda _ref: csv_path)
    # Force ``_load_all`` down the CSV branch by making the YAML lookup
    # look like the CDM has no packaged YAML directory.
    from ovid2.cdm import yaml_loader as _yl

    def _no_yaml(_name: str):
        raise _yl.CdmYamlError("test: YAML disabled for this fixture")

    monkeypatch.setattr(_yl, "cdm_yaml_dir", _no_yaml)
    clear_cache()


@pytest.mark.unit
class TestLoad:
    def test_load_cdm_table_columns_returns_ordered_uppercase(self) -> None:
        cols = load_cdm_table_columns("pcornet_7_0.HASH_TOKEN")
        assert cols == ["PATID", "TOKEN_ENCRYPTION_KEY_ID", "TOKEN_01"]

    def test_load_cdm_full_returns_typed_rows(self) -> None:
        rows = load_cdm_full("pcornet_7_0.DEMOGRAPHIC")
        assert len(rows) == 3
        assert rows[0] == CdmField(table="DEMOGRAPHIC", name="PATID", rdbms_type="RDBMS Text(x)")
        assert rows[1].rdbms_type == "RDBMS Date"
        assert rows[2].rdbms_type == "RDBMS Text(2)"


@pytest.mark.unit
class TestSnowflakeTypeFor:
    def test_declared_text_width_wins(self) -> None:
        f = CdmField(table="HASH_TOKEN", name="TOKEN_01", rdbms_type="RDBMS Text(256)")
        assert _snowflake_type_for(f) == "VARCHAR(256)"

    def test_variable_text_uses_harmonized_map(self) -> None:
        # PATID is in HARMONIZED_FIELD_SIZES at 64.
        f = CdmField(table="HASH_TOKEN", name="PATID", rdbms_type="RDBMS Text(x)")
        assert _snowflake_type_for(f) == "VARCHAR(64)"
        # ENCOUNTERID is in HARMONIZED_FIELD_SIZES at 128.
        f = CdmField(table="LAB_RESULT_CM", name="ENCOUNTERID", rdbms_type="RDBMS Text(x)")
        assert _snowflake_type_for(f) == "VARCHAR(128)"

    def test_variable_text_without_harmonized_entry_uses_default(self) -> None:
        f = CdmField(table="LAB_RESULT_CM", name="LAB_RESULT_CM_ID", rdbms_type="RDBMS Text(x)")
        # Not in HARMONIZED_FIELD_SIZES → fallback 256.
        assert _snowflake_type_for(f) == "VARCHAR(256)"

    def test_rdbms_date_maps_to_date(self) -> None:
        f = CdmField(table="DEMOGRAPHIC", name="BIRTH_DATE", rdbms_type="RDBMS Date")
        assert _snowflake_type_for(f) == "DATE"

    def test_rdbms_number_maps_to_number(self) -> None:
        f = CdmField(table="LAB_RESULT_CM", name="RESULT_NUM", rdbms_type="RDBMS Number(x)")
        assert _snowflake_type_for(f) == "NUMBER(38,10)"

    def test_unknown_type_falls_back_to_default_varchar(self, caplog: pytest.LogCaptureFixture) -> None:
        f = CdmField(table="ODDBALL", name="MYSTERY", rdbms_type="SomethingUnknown")
        result = _snowflake_type_for(f)
        assert result == "VARCHAR(256)"
        assert any("Unrecognised CDM RDBMS_DATA_TYPE" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
class TestBuildDdl:
    def test_hash_token_ddl_uses_harmonized_and_declared_widths(self) -> None:
        ddl = build_snowflake_types_ddl("pcornet_7_0.HASH_TOKEN")
        # PATID is harmonised (VARCHAR(64)), TOKEN_ENCRYPTION_KEY_ID is declared 50,
        # TOKEN_01 is declared 256.
        assert ddl == (
            "PATID VARCHAR(64), "
            "TOKEN_ENCRYPTION_KEY_ID VARCHAR(50), "
            "TOKEN_01 VARCHAR(256)"
        )

    def test_demographic_ddl_mixes_types(self) -> None:
        ddl = build_snowflake_types_ddl("pcornet_7_0.DEMOGRAPHIC")
        assert ddl == "PATID VARCHAR(64), BIRTH_DATE DATE, SEX VARCHAR(2)"

    def test_lab_result_ddl_includes_number(self) -> None:
        ddl = build_snowflake_types_ddl("pcornet_7_0.LAB_RESULT_CM")
        assert "RESULT_NUM NUMBER(38,10)" in ddl
        assert "ENCOUNTERID VARCHAR(128)" in ddl

    def test_missing_table_returns_empty_string(self) -> None:
        ddl = build_snowflake_types_ddl("pcornet_7_0.DOES_NOT_EXIST")
        assert ddl == ""


class TestBuildSnowflakeTypedColumns:
    def test_returns_ordered_name_type_pairs(self) -> None:
        from ovid2.schema.cdm import build_snowflake_typed_columns

        cols = build_snowflake_typed_columns("pcornet_7_0.LAB_RESULT_CM")
        assert cols, "expected non-empty typed columns for LAB_RESULT_CM"
        as_dict = dict(cols)
        # Numeric column carries NUMBER; id columns carry VARCHAR widths.
        assert as_dict["RESULT_NUM"] == "NUMBER(38,10)"
        assert as_dict["ENCOUNTERID"] == "VARCHAR(128)"
        # Every entry is an uppercase (name, type) tuple.
        assert all(
            isinstance(n, str) and n == n.upper() and isinstance(t, str)
            for n, t in cols
        )

    def test_missing_table_returns_empty_list(self) -> None:
        from ovid2.schema.cdm import build_snowflake_typed_columns

        assert build_snowflake_typed_columns("pcornet_7_0.DOES_NOT_EXIST") == []


class TestWhitespaceInRdbmsType:
    def test_text_space_paren_x_uses_harmonized_id(self) -> None:
        # ``RDBMS Text (x)`` (space before paren) must be treated like
        # ``RDBMS Text(x)`` — PATID routes through the harmonized-ID map.
        f = CdmField(table="DEATH", name="PATID", rdbms_type="RDBMS Text (x)")
        assert _snowflake_type_for(f) == "VARCHAR(64)"

    def test_text_space_paren_x_non_id_defaults(self) -> None:
        f = CdmField(
            table="IMMUNIZATION", name="VX_BODY_SITE", rdbms_type="RDBMS Text (x)"
        )
        assert _snowflake_type_for(f) == "VARCHAR(256)"

    def test_text_space_paren_width(self) -> None:
        f = CdmField(table="T", name="C", rdbms_type="RDBMS Text (8)")
        assert _snowflake_type_for(f) == "VARCHAR(8)"


class TestBuildSnowflakeWriteSchema:
    def test_hash_token_kinds_and_widths(self) -> None:
        from ovid2.schema.cdm import build_snowflake_write_schema

        spec = {
            name: (kind, width)
            for name, kind, width in build_snowflake_write_schema(
                "pcornet_7_0.HASH_TOKEN"
            )
        }
        # PATID harmonised to 64; declared widths otherwise.
        assert spec["PATID"] == ("varchar", 64)
        assert spec["TOKEN_ENCRYPTION_KEY_ID"] == ("varchar", 50)
        assert spec["TOKEN_01"] == ("varchar", 256)

    def test_demographic_date_kind(self) -> None:
        from ovid2.schema.cdm import build_snowflake_write_schema

        spec = {
            name: (kind, width)
            for name, kind, width in build_snowflake_write_schema(
                "pcornet_7_0.DEMOGRAPHIC"
            )
        }
        assert spec["BIRTH_DATE"] == ("date", 0)
        assert spec["SEX"] == ("varchar", 2)
        assert spec["PATID"] == ("varchar", 64)

    def test_number_kind(self) -> None:
        from ovid2.schema.cdm import build_snowflake_write_schema

        spec = {
            name: (kind, width)
            for name, kind, width in build_snowflake_write_schema(
                "pcornet_7_0.LAB_RESULT_CM"
            )
        }
        assert spec["RESULT_NUM"][0] == "number"
        # Text(x) non-ID column falls back to the default VARCHAR width.
        assert spec["LAB_RESULT_CM_ID"] == ("varchar", 256)

    def test_missing_table_returns_empty(self) -> None:
        from ovid2.schema.cdm import build_snowflake_write_schema

        assert build_snowflake_write_schema("pcornet_7_0.DOES_NOT_EXIST") == []
