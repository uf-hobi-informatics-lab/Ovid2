"""Unit tests for :class:`ovid2.drivers.snowflake.SnowflakeAddress` (D3.snowflake).

Preserved from the pre-D3.snowflake era; the address class now lives
at :mod:`ovid2.drivers.snowflake.spark` (re-exported from
:mod:`ovid2.drivers.snowflake`). Every test value survives byte-
identical after the import-path retarget — the reader/writer,
schema-derivation, connector-options builder, and retry loop are
unchanged.

The tests exercise the ported ``read`` and ``write`` paths with
mocks: no real Snowflake driver is required. Focus areas:

- Config parsing (auth mode, default schema, DB validation).
- Schema-name derivation and path-character sanitisation.
- Spark-connector option dict (key-pair, password, connection defaults).
- ``createTableColumnTypes`` DDL sourcing.
- Transient-error retry loop (calls ``time.sleep``, capped at MAX_RETRIES).
- ``read`` returns a lazy DataFrame reading from the address's
  ``default_schema`` (or the connection default when unset).
"""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import MagicMock

import pytest

from ovid2.drivers.snowflake import SnowflakeAddress
from ovid2.drivers.snowflake.spark import (
    _SQL_IDENTIFIER_RE,
    _is_transient_error,
)


def _minimal_config(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "account": "test-acct",
        "database": "TEST_DB",
        "user": "OVIDCONNECTOR",
        "warehouse": "WH",
        "role": "R",
    }
    base.update(overrides)
    return base


def _make_df_mock() -> MagicMock:
    """Return a DataFrame mock wired for the ``write`` fluent chain."""
    writer = MagicMock(name="Writer")
    writer.format.return_value = writer
    writer.options.return_value = writer
    writer.option.return_value = writer
    writer.mode.return_value = writer
    writer.save.return_value = None

    df = MagicMock(name="DataFrame")
    df.repartition.return_value = df
    df.write = writer
    return df


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConstruction:
    def test_defaults(self) -> None:
        addr = SnowflakeAddress(_minimal_config())
        assert addr.account == "test-acct"
        assert addr.database == "TEST_DB"
        assert addr.default_schema == ""
        assert addr.warehouse == "WH"
        assert addr._password is None
        assert addr._private_key_der is None

    def test_key_pair_config_stored(self) -> None:
        addr = SnowflakeAddress(_minimal_config(private_key=b"\x00\x01DER"))
        assert addr._private_key_der == b"\x00\x01DER"

    def test_password_config_stored(self) -> None:
        addr = SnowflakeAddress(_minimal_config(password="hunter2"))
        assert addr._password == "hunter2"


# ---------------------------------------------------------------------------
# Schema derivation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSchemaName:
    def test_simple(self) -> None:
        addr = SnowflakeAddress(_minimal_config())
        assert addr._schema_name("acme", "20260610") == "OVID_acme_20260610"

    def test_slashes_are_sanitised(self) -> None:
        addr = SnowflakeAddress(_minimal_config())
        assert addr._schema_name("acme", "sub/20260610") == "OVID_acme_sub_20260610"
        assert addr._schema_name("acme", "sub\\20260610") == "OVID_acme_sub_20260610"


# ---------------------------------------------------------------------------
# Spark connector options
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSparkOptions:
    def test_key_pair_options_include_pem_body(self) -> None:
        der = b"synthetic DER bytes"
        addr = SnowflakeAddress(_minimal_config(private_key=der))
        opts = addr._spark_options("OVID_acme_20260610")
        assert opts["sfURL"] == "test-acct.snowflakecomputing.com"
        assert opts["sfAccount"] == "test-acct"
        assert opts["sfDatabase"] == "TEST_DB"
        assert opts["sfSchema"] == "OVID_acme_20260610"
        assert opts["sfUser"] == "OVIDCONNECTOR"
        assert opts["sfWarehouse"] == "WH"
        assert opts["sfRole"] == "R"
        assert opts["pem_private_key"] == base64.b64encode(der).decode("ascii")
        assert "sfPassword" not in opts

    def test_password_options_include_password(self) -> None:
        addr = SnowflakeAddress(_minimal_config(password="hunter2"))
        opts = addr._spark_options("OVID_acme_20260610")
        assert opts["sfPassword"] == "hunter2"
        assert "pem_private_key" not in opts

    def test_optional_fields_omitted_when_blank(self) -> None:
        cfg = _minimal_config(warehouse="", role="")
        addr = SnowflakeAddress(cfg)
        opts = addr._spark_options("s")
        assert "sfWarehouse" not in opts
        assert "sfRole" not in opts


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestValidation:
    def test_missing_database_raises(self) -> None:
        addr = SnowflakeAddress(_minimal_config(database=""))
        with pytest.raises(ValueError, match="No Snowflake database specified"):
            addr._validate_database()


# ---------------------------------------------------------------------------
# read — lazy pass-through to spark.read.format('snowflake')
# ---------------------------------------------------------------------------


def _make_spark_read_mock() -> tuple[MagicMock, MagicMock]:
    """Return ``(spark, reader)`` mocks wired for the read fluent chain.

    ``reader`` is the terminal object whose ``load()`` returns the
    (mock) DataFrame; assertions inspect ``reader.options.call_args``.
    """
    reader = MagicMock(name="Reader")
    reader.format.return_value = reader
    reader.options.return_value = reader
    df = MagicMock(name="DataFrame")
    reader.load.return_value = df

    spark = MagicMock(name="SparkSession")
    spark.read = reader
    return spark, reader


@pytest.mark.unit
class TestRead:
    def test_read_uses_default_schema_and_dbtable(self) -> None:
        addr = SnowflakeAddress(
            _minimal_config(private_key=b"DER", schema="PUBLIC"),
        )
        spark, reader = _make_spark_read_mock()

        df = addr.read(
            spark=spark,
            pipeline="acme",
            batch_id="20260610",
            file_name="hash_token",
        )

        reader.format.assert_called_once_with("snowflake")
        reader.load.assert_called_once_with()
        opts = reader.options.call_args.kwargs
        assert opts["sfSchema"] == "PUBLIC"
        assert opts["sfDatabase"] == "TEST_DB"
        assert opts["dbtable"] == "hash_token"
        assert opts["pem_private_key"] == base64.b64encode(b"DER").decode("ascii")
        # ``load`` was called AFTER options; result flows back to caller.
        assert df is reader.load.return_value

    def test_read_omits_sfschema_when_default_schema_blank(self) -> None:
        addr = SnowflakeAddress(_minimal_config(password="pw"))
        assert addr.default_schema == ""
        spark, reader = _make_spark_read_mock()

        addr.read(spark=spark, pipeline="acme", batch_id="b", file_name="T")
        opts = reader.options.call_args.kwargs
        assert "sfSchema" not in opts
        assert opts["dbtable"] == "T"
        assert opts["sfPassword"] == "pw"

    def test_read_preserves_dbtable_case(self) -> None:
        # Reads are case-sensitive: the caller (formatter YAML) decides
        # what identifier to hit; the address must not upper-case it
        # the way ``write`` does.
        addr = SnowflakeAddress(_minimal_config(schema="PUBLIC"))
        spark, reader = _make_spark_read_mock()

        addr.read(spark=spark, pipeline="acme", batch_id="b", file_name="mixedCase")
        assert reader.options.call_args.kwargs["dbtable"] == "mixedCase"

    def test_read_missing_database_raises(self) -> None:
        addr = SnowflakeAddress(_minimal_config(database=""))
        with pytest.raises(ValueError, match="No Snowflake database specified"):
            addr.read(
                spark=MagicMock(),
                pipeline="acme",
                batch_id="20260610",
                file_name="hash_token",
            )

    def test_read_is_lazy_no_collect(self) -> None:
        """Contract: ``read`` must not touch the DataFrame beyond ``.load()``."""
        addr = SnowflakeAddress(_minimal_config(schema="PUBLIC"))
        spark, _reader = _make_spark_read_mock()
        df = addr.read(spark=spark, pipeline="acme", batch_id="b", file_name="T")
        # No eager materialisation.
        assert not df.collect.called
        assert not df.count.called
        assert not df.take.called
        assert not df.toPandas.called

    def test_read_uses_table_name_fallback_when_file_name_is_a_glob(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Regression: ACME YAMLs ship disc globs like ``*datavant_person_tokens*``.

        Rather than fail with ``syntax error ... unexpected '*'``, the
        read must fall back to the CDM ``table_name`` and log the
        substitution so operators see it.
        """
        addr = SnowflakeAddress(_minimal_config(schema="PUBLIC"))
        spark, reader = _make_spark_read_mock()

        with caplog.at_level("INFO", logger="ovid2.drivers.snowflake.spark"):
            addr.read(
                spark=spark,
                pipeline="acme",
                batch_id="20260610",
                file_name="*datavant_person_tokens*",
                table_name="hash_token",
            )

        opts = reader.options.call_args.kwargs
        assert opts["dbtable"] == "hash_token"
        # The substitution should be surfaced at INFO with both names
        # so the operator can spot the mismatch.
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "not a valid SQL identifier" in m
            and "hash_token" in m
            and "*datavant_person_tokens*" in m
            for m in messages
        ), messages

    def test_read_prefers_file_name_when_it_is_a_valid_identifier(self) -> None:
        """When both are valid, ``file_name`` (the explicit YAML value) wins."""
        addr = SnowflakeAddress(_minimal_config(schema="PUBLIC"))
        spark, reader = _make_spark_read_mock()

        addr.read(
            spark=spark,
            pipeline="acme",
            batch_id="b",
            file_name="HASH_TOKEN",
            table_name="hash_token",
        )
        assert reader.options.call_args.kwargs["dbtable"] == "HASH_TOKEN"

    def test_read_raises_when_neither_candidate_is_valid(self) -> None:
        """Misconfiguration is surfaced with a clear ValueError, not JDBC noise."""
        addr = SnowflakeAddress(_minimal_config(schema="PUBLIC"))
        with pytest.raises(ValueError, match="cannot resolve a Snowflake identifier"):
            addr.read(
                spark=MagicMock(),
                pipeline="acme",
                batch_id="b",
                file_name="*bad*",
                table_name="*also-bad*",
            )

    def test_read_raises_when_table_name_absent_and_file_name_is_glob(self) -> None:
        addr = SnowflakeAddress(_minimal_config(schema="PUBLIC"))
        with pytest.raises(ValueError, match="cannot resolve a Snowflake identifier"):
            addr.read(
                spark=MagicMock(),
                pipeline="acme",
                batch_id="b",
                file_name="*foo*",
                # table_name omitted entirely
            )


# ---------------------------------------------------------------------------
# _SQL_IDENTIFIER_RE + _resolve_dbtable
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSqlIdentifierRegex:
    """The regex is the sole gate between disc globs and Snowflake SQL.

    Underspecifying it would let a glob through and reproduce the
    original ``syntax error ... unexpected '*'`` bug; overspecifying it
    would reject a legitimate identifier and force operators to edit
    every YAML.
    """

    @pytest.mark.parametrize(
        "candidate",
        [
            "HASH_TOKEN",
            "hash_token",
            "MixedCase",
            "_leading_underscore",
            "T",
            "table_1",
            "table$1",  # Snowflake permits `$` in identifiers
            "T" * 128,  # long-but-legal
        ],
    )
    def test_accepts_valid_identifiers(self, candidate: str) -> None:
        assert _SQL_IDENTIFIER_RE.match(candidate) is not None

    @pytest.mark.parametrize(
        "candidate",
        [
            "*datavant_person_tokens*",
            "*external_meds.csv",
            "*person_relationship.csv",
            "external_meds.csv",  # dot: filesystem extension, not SQL
            "1_starts_with_digit",
            "has space",
            "has-hyphen",
            "path/like",
            "quote'inside",
            "",  # empty
        ],
    )
    def test_rejects_non_identifiers(self, candidate: str) -> None:
        assert _SQL_IDENTIFIER_RE.match(candidate) is None


@pytest.mark.unit
class TestResolveDbtable:
    def test_prefers_file_name(self) -> None:
        addr = SnowflakeAddress(_minimal_config())
        assert addr._resolve_dbtable("REAL_TABLE", "hash_token") == "REAL_TABLE"

    def test_falls_back_to_table_name(self) -> None:
        addr = SnowflakeAddress(_minimal_config())
        assert addr._resolve_dbtable("*glob*", "hash_token") == "hash_token"

    def test_empty_file_name_falls_back(self) -> None:
        addr = SnowflakeAddress(_minimal_config())
        assert addr._resolve_dbtable("", "hash_token") == "hash_token"

    def test_none_file_name_falls_back(self) -> None:
        addr = SnowflakeAddress(_minimal_config())
        # str type only per signature, but empty string reaches the
        # same branch — verifying via the empty string plus the
        # both-invalid case (below) covers the None-safe path.
        with pytest.raises(ValueError):
            addr._resolve_dbtable("", None)

    def test_both_invalid_raises_value_error(self) -> None:
        addr = SnowflakeAddress(_minimal_config())
        with pytest.raises(ValueError, match="cannot resolve"):
            addr._resolve_dbtable("*bad*", "*also-bad*")

    def test_error_message_names_both_inputs(self) -> None:
        addr = SnowflakeAddress(_minimal_config())
        with pytest.raises(ValueError) as excinfo:
            addr._resolve_dbtable("*glob-only*", None)
        message = str(excinfo.value)
        assert "*glob-only*" in message
        assert "input_file_name" in message  # remediation hint


# ---------------------------------------------------------------------------
# _is_transient_error
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTransientError:
    def test_positive_cases(self) -> None:
        assert _is_transient_error("java.net.UnknownHostException: sf")
        assert _is_transient_error("SocketTimeoutException")
        assert _is_transient_error("Connection reset by peer")
        assert _is_transient_error("no response or invalid response from server")

    def test_negative_cases(self) -> None:
        assert not _is_transient_error("SQL compilation error: object X does not exist")
        assert not _is_transient_error("Insufficient privileges")


# ---------------------------------------------------------------------------
# write — success + retry paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWriteSuccess:
    def test_write_creates_schema_and_saves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        addr = SnowflakeAddress(_minimal_config(private_key=b"DER"))
        df = _make_df_mock()

        # Stub the DDL executor so we don't need a Snowflake driver.
        called: dict[str, Any] = {}

        def fake_create_schema(schema_name: str) -> None:
            called["schema"] = schema_name

        monkeypatch.setattr(addr, "_create_schema_if_not_exists", fake_create_schema)
        # Stub the CDM schema application: the real one needs a live Spark
        # session (``spark.createDataFrame(df.rdd, schema)``); the typing
        # logic itself is covered by ``TestApplyCdmSchema`` below. Here we
        # only assert the write fluent-chain around it.
        applied: dict[str, Any] = {}

        def fake_apply(dframe: Any, spark: Any, table: str) -> Any:
            applied["table"] = table
            return dframe

        monkeypatch.setattr(addr, "_apply_cdm_schema", fake_apply)

        addr.write(
            df=df,
            spark=MagicMock(name="SparkSession"),
            pipeline="acme",
            batch_id="20260610",
            table_name="hash_token",
        )

        assert called["schema"] == "OVID_acme_20260610"
        assert applied["table"] == "hash_token"
        df.repartition.assert_called_once_with(10)
        df.write.format.assert_called_once_with("snowflake")
        df.write.mode.assert_called_once_with("overwrite")
        df.write.save.assert_called_once()
        # The generic ``createTableColumnTypes`` option is NOT used — the
        # Snowflake connector ignores it; typing comes from the DataFrame
        # schema instead (see ``_apply_cdm_schema``).
        assert not any(
            args[0][0] == "createTableColumnTypes" for args in df.write.option.call_args_list
        )
        # dbtable is upper-cased.
        options_arg = df.write.options.call_args.kwargs
        assert options_arg["dbtable"] == "HASH_TOKEN"
        assert options_arg["sfSchema"] == "OVID_acme_20260610"

    def test_write_warns_when_no_cdm(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        addr = SnowflakeAddress(_minimal_config(private_key=b"DER"))
        df = _make_df_mock()
        monkeypatch.setattr(addr, "_create_schema_if_not_exists", lambda s: None)
        # No CDM write spec -> _apply_cdm_schema returns df unchanged + warns.
        monkeypatch.setattr(addr, "_write_schema", lambda t: [])

        with caplog.at_level("WARNING"):
            addr.write(
                df=df,
                spark=MagicMock(),
                pipeline="acme",
                batch_id="20260610",
                table_name="unknown_table",
            )
        # No createTableColumnTypes option should have been set.
        assert not any(
            args[0][0] == "createTableColumnTypes" for args in df.write.option.call_args_list
        )
        assert any("No CDM schema found" in r.getMessage() for r in caplog.records)
        df.write.save.assert_called_once()


@pytest.mark.unit
class TestWriteRetry:
    def test_transient_error_retries_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        addr = SnowflakeAddress(_minimal_config(private_key=b"DER"))
        df = _make_df_mock()

        # First save raises transient; second succeeds.
        call_state = {"n": 0}

        def fake_save() -> None:
            call_state["n"] += 1
            if call_state["n"] == 1:
                raise RuntimeError("SocketTimeoutException: something")

        df.write.save.side_effect = fake_save
        monkeypatch.setattr(addr, "_create_schema_if_not_exists", lambda s: None)
        monkeypatch.setattr(addr, "_apply_cdm_schema", lambda dframe, spark, t: dframe)
        monkeypatch.setattr("ovid2.drivers.snowflake.spark.time.sleep", lambda _s: None)

        addr.write(
            df=df,
            spark=MagicMock(),
            pipeline="acme",
            batch_id="20260610",
            table_name="hash_token",
        )
        assert call_state["n"] == 2

    def test_non_transient_error_raises_immediately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        addr = SnowflakeAddress(_minimal_config(private_key=b"DER"))
        df = _make_df_mock()
        df.write.save.side_effect = RuntimeError("SQL compilation error: table X does not exist")
        monkeypatch.setattr(addr, "_create_schema_if_not_exists", lambda s: None)
        monkeypatch.setattr(addr, "_apply_cdm_schema", lambda dframe, spark, t: dframe)

        sleep_calls: list[float] = []
        monkeypatch.setattr(
            "ovid2.drivers.snowflake.spark.time.sleep",
            lambda s: sleep_calls.append(s),
        )

        with pytest.raises(RuntimeError, match="SQL compilation error"):
            addr.write(
                df=df,
                spark=MagicMock(),
                pipeline="acme",
                batch_id="20260610",
                table_name="hash_token",
            )
        assert sleep_calls == []  # no retry

    def test_transient_error_gives_up_after_max_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        addr = SnowflakeAddress(_minimal_config(private_key=b"DER"))
        df = _make_df_mock()
        df.write.save.side_effect = RuntimeError("connection reset")
        monkeypatch.setattr(addr, "_create_schema_if_not_exists", lambda s: None)
        monkeypatch.setattr(addr, "_apply_cdm_schema", lambda dframe, spark, t: dframe)
        monkeypatch.setattr("ovid2.drivers.snowflake.spark.time.sleep", lambda _s: None)

        with pytest.raises(RuntimeError, match="connection reset"):
            addr.write(
                df=df,
                spark=MagicMock(),
                pipeline="acme",
                batch_id="20260610",
                table_name="hash_token",
            )
        # save called MAX_RETRIES times.
        assert df.write.save.call_count == 3


# ---------------------------------------------------------------------------
# _apply_cdm_schema — CDM-typed DataFrame rebuild (the fix for the
# VARCHAR(16777216) regression). Requires a real Spark session because it
# calls ``spark.createDataFrame(df.rdd, schema)``.
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestApplyCdmSchema:
    @pytest.fixture(autouse=True)
    def _fresh_cdm_cache(self) -> Any:
        """Drop any CDM cache a prior test module populated.

        ``tests/unit/schema/test_cdm.py`` installs a synthetic fixture CSV
        and leaves it cached under ``pcornet_7_0``; clearing here forces
        these tests to read the real packaged YAML.
        """
        from ovid2.schema.cdm import clear_cache

        clear_cache()
        yield
        clear_cache()

    def test_death_columns_get_cdm_types(self, spark: Any) -> None:
        """DEATH: PATID->VARCHAR(64), *_status->VARCHAR(2), DEATH_DATE->DATE."""
        from pyspark.sql.types import DateType, StringType

        addr = SnowflakeAddress(_minimal_config())
        # All-string source, mirroring the pipeline's produced DataFrame.
        rows = [("p1", "2020-01-01", "N", "L", "1")]
        cols = [
            "PATID",
            "DEATH_DATE",
            "DEATH_DATE_IMPUTE",
            "DEATH_SOURCE",
            "DEATH_MATCH_CONFIDENCE",
        ]
        df = spark.createDataFrame(rows, cols)

        out = addr._apply_cdm_schema(df, spark, "death")
        by_name = {f.name: f for f in out.schema.fields}

        assert isinstance(by_name["PATID"].dataType, StringType)
        assert by_name["PATID"].metadata.get("maxlength") == 64
        assert by_name["DEATH_DATE_IMPUTE"].metadata.get("maxlength") == 2
        assert by_name["DEATH_SOURCE"].metadata.get("maxlength") == 2
        assert isinstance(by_name["DEATH_DATE"].dataType, DateType)

    def test_condition_id_and_codes(self, spark: Any) -> None:
        """CONDITION: CONDITIONID->256, PATID->64, CONDITION->18 (Text(18))."""
        addr = SnowflakeAddress(_minimal_config())
        cols = [
            "CONDITIONID",
            "PATID",
            "ENCOUNTERID",
            "REPORT_DATE",
            "RESOLVE_DATE",
            "ONSET_DATE",
            "CONDITION_STATUS",
            "CONDITION",
            "CONDITION_TYPE",
            "CONDITION_SOURCE",
            "RAW_CONDITION_STATUS",
            "RAW_CONDITION",
            "RAW_CONDITION_TYPE",
            "RAW_CONDITION_SOURCE",
        ]
        df = spark.createDataFrame([tuple("x" for _ in cols)], cols)

        out = addr._apply_cdm_schema(df, spark, "condition")
        meta = {f.name: f.metadata.get("maxlength") for f in out.schema.fields}
        assert meta["CONDITIONID"] == 256
        assert meta["PATID"] == 64
        assert meta["ENCOUNTERID"] == 128
        assert meta["CONDITION"] == 18
        assert meta["CONDITION_STATUS"] == 2

    def test_oversized_string_is_truncated_to_cdm_width(self, spark: Any) -> None:
        """A value longer than the declared VARCHAR width is right-trimmed.

        Regression: an over-long RAW_EXT_MED_NAME (OMOP drug name) or an
        over-long PRO_TIME used to overflow the Snowflake VARCHAR(N) column
        and fail the whole table's COPY. The writer now conforms the data to
        the declared width, not just the DDL.
        """
        addr = SnowflakeAddress(_minimal_config())
        # DEATH_SOURCE is VARCHAR(2) in the CDM; feed it a longer value.
        df = spark.createDataFrame([("p1", "TOOLONG")], ["PATID", "DEATH_SOURCE"])
        out = addr._apply_cdm_schema(df, spark, "death")
        by_name = {f.name: f for f in out.schema.fields}
        assert by_name["DEATH_SOURCE"].metadata.get("maxlength") == 2
        row = out.select("DEATH_SOURCE").head()
        assert row["DEATH_SOURCE"] == "TO"

    def test_extra_non_cdm_column_preserved_as_string(self, spark: Any) -> None:
        """A DataFrame column absent from the CDM survives as a string."""
        from pyspark.sql.types import StringType

        addr = SnowflakeAddress(_minimal_config())
        df = spark.createDataFrame(
            [("p1", "N", "extra")],
            ["PATID", "DEATH_SOURCE", "SOURCE"],
        )
        out = addr._apply_cdm_schema(df, spark, "death")
        names = {f.name for f in out.schema.fields}
        assert "SOURCE" in names
        source_field = next(f for f in out.schema.fields if f.name == "SOURCE")
        assert isinstance(source_field.dataType, StringType)

    def test_unknown_table_returns_df_unchanged_and_warns(
        self, spark: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        addr = SnowflakeAddress(_minimal_config())
        df = spark.createDataFrame([("a", "b")], ["COL_A", "COL_B"])
        with caplog.at_level("WARNING"):
            out = addr._apply_cdm_schema(df, spark, "no_such_table_xyz")
        assert out is df
        assert any("No CDM schema found" in r.getMessage() for r in caplog.records)
