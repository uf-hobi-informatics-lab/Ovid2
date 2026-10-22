"""Unit tests for :class:`ovid2.drivers.snowflake.staging.databricks.SnowparkDatabricksStrategy`.

Every test drives the strategy through fake Snowpark session doubles
plus stubbed ``databricks.sql`` and ``boto3`` modules so the suite
runs without live Snowflake / AWS / Databricks credentials. End-to-end
verification against real workspaces is deferred to the operator's
environment; the strategy's Snowflake-side SQL surface matches Phase
C/D machinery which was live-verified byte-for-byte on a real
Snowflake account.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from ovid2.core.engines.stager import (
    SnowparkEngineStager,
    SnowparkStagingStrategy,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Every test gets a deterministic instance id + clean env."""
    monkeypatch.setenv("OVID2_INSTANCE_ID", "20260806-140000-def456")
    monkeypatch.delenv("OVID2_SNOWPARK_STAGING_KEEP", raising=False)
    monkeypatch.delenv("OVID2_SNOWPARK_STAGING_S3_INTEGRATION", raising=False)
    monkeypatch.delenv("OVID2_SNOWPARK_STAGING_SCHEMA", raising=False)
    monkeypatch.delenv(
        "OVID2_SNOWPARK_STAGING_DATABRICKS_BOUNCE_URI", raising=False,
    )
    monkeypatch.delenv(
        "OVID2_SNOWPARK_STAGING_DATABRICKS_BOUNCE_S3_INTEGRATION",
        raising=False,
    )


@pytest.fixture(autouse=True)
def _clear_strategy_state(monkeypatch):
    """Reset the strategy's class-level cleanup registries.

    Also globally stubs :func:`atexit.register` to a no-op so
    ``TestMaterialise`` / ``TestPersist`` don't leave real cleanup
    hooks registered for after-pytest-shutdown execution (which
    fires boto3 calls against long-gone stub modules). Tests that
    explicitly want to inspect the atexit interaction override
    ``atexit.register`` locally with a capturing list.
    """
    from ovid2.drivers.snowflake.staging.databricks import (
        SnowparkDatabricksStrategy,
    )

    monkeypatch.setattr("atexit.register", lambda *a, **kw: None)
    SnowparkDatabricksStrategy._cleanups_scheduled.clear()
    SnowparkDatabricksStrategy._bounce_prefixes.clear()
    yield
    SnowparkDatabricksStrategy._cleanups_scheduled.clear()
    SnowparkDatabricksStrategy._bounce_prefixes.clear()


class _FakeSession:
    """A Snowpark session double capturing every side effect."""

    def __init__(self) -> None:
        self.sql_calls: list[str] = []
        self.tables_returned: dict[str, Any] = {}

    def sql(self, query: str) -> Any:
        self.sql_calls.append(query)
        result = MagicMock()
        result.collect.return_value = []
        return result

    def table(self, fqtn: str) -> Any:
        df = self.tables_returned.setdefault(fqtn, MagicMock(name=f"df:{fqtn}"))
        return df

    def get_current_database(self) -> str:
        return "TESTDB"

    def get_current_schema(self) -> str | None:
        return None

    @property
    def native(self) -> "Any":
        return self


class _FakeDatabricksDriver:
    """Stand-in for the Databricks driver returning a fixed merged config."""

    type_name = "databricks"

    def __init__(self, merged: dict[str, Any]) -> None:
        self._merged = merged

    def as_legacy_src_args(self) -> tuple[str, dict[str, Any]]:
        return "databricks", dict(self._merged)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_strategy_registered_under_databricks(self) -> None:
        import ovid2.drivers.snowflake  # noqa: F401

        assert "databricks" in SnowparkEngineStager._STRATEGIES
        assert SnowparkEngineStager.supports("databricks") is True

    def test_registered_class_is_a_snowpark_strategy(self) -> None:
        import ovid2.drivers.snowflake  # noqa: F401

        cls = SnowparkEngineStager._STRATEGIES["databricks"]
        assert issubclass(cls, SnowparkStagingStrategy)
        assert cls.driver_type == "databricks"


# ---------------------------------------------------------------------------
# Import safety — no heavy SDKs at module scope
# ---------------------------------------------------------------------------


class TestImportSafety:
    def test_module_scope_does_not_import_boto3(self) -> None:
        for mod in list(sys.modules):
            if mod.startswith(("boto3", "botocore")):
                del sys.modules[mod]
            if mod == "ovid2.drivers.snowflake.staging.databricks":
                del sys.modules[mod]
        import ovid2.drivers.snowflake.staging.databricks  # noqa: F401

        leaks = [m for m in sys.modules if m.startswith(("boto3", "botocore"))]
        assert leaks == [], f"boto3/botocore leaked: {leaks}"

    def test_module_scope_does_not_import_databricks_sql(self) -> None:
        for mod in list(sys.modules):
            if mod == "ovid2.drivers.snowflake.staging.databricks":
                del sys.modules[mod]
            if mod.startswith("databricks."):
                del sys.modules[mod]
        import ovid2.drivers.snowflake.staging.databricks  # noqa: F401

        leaks = [m for m in sys.modules if m.startswith("databricks.")]
        assert leaks == [], f"databricks.sql leaked: {leaks}"

    def test_module_scope_does_not_import_pyspark(self) -> None:
        for mod in list(sys.modules):
            if mod == "ovid2.drivers.snowflake.staging.databricks":
                del sys.modules[mod]
        pyspark_before = {m for m in sys.modules if m.startswith("pyspark")}
        import ovid2.drivers.snowflake.staging.databricks  # noqa: F401
        pyspark_after = {m for m in sys.modules if m.startswith("pyspark")}
        assert pyspark_after == pyspark_before

    def test_module_scope_does_not_import_snowpark(self) -> None:
        for mod in list(sys.modules):
            if mod == "ovid2.drivers.snowflake.staging.databricks":
                del sys.modules[mod]
        snowpark_before = {
            m for m in sys.modules if m.startswith("snowflake.snowpark")
        }
        import ovid2.drivers.snowflake.staging.databricks  # noqa: F401
        snowpark_after = {
            m for m in sys.modules if m.startswith("snowflake.snowpark")
        }
        assert snowpark_after == snowpark_before


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_table_stem_bare(self) -> None:
        from ovid2.drivers.snowflake.staging.databricks import _table_stem

        assert _table_stem("ENCOUNTER") == "ENCOUNTER"

    def test_table_stem_dotted(self) -> None:
        from ovid2.drivers.snowflake.staging.databricks import _table_stem

        assert _table_stem("main.public.ENCOUNTER") == "ENCOUNTER"
        assert _table_stem("schema.T") == "T"

    def test_table_stem_empty(self) -> None:
        from ovid2.drivers.snowflake.staging.databricks import _table_stem

        assert _table_stem("") == ""

    def test_qualify_table_three_parts(self) -> None:
        from ovid2.drivers.snowflake.staging.databricks import _qualify_table

        out = _qualify_table({}, "main.public.ENCOUNTER")
        assert out == "`main`.`public`.`ENCOUNTER`"

    def test_qualify_table_two_parts_uses_catalog_fallback(self) -> None:
        from ovid2.drivers.snowflake.staging.databricks import _qualify_table

        out = _qualify_table({"catalog": "main"}, "public.ENCOUNTER")
        assert out == "`main`.`public`.`ENCOUNTER`"

    def test_qualify_table_bare_uses_both_fallbacks(self) -> None:
        from ovid2.drivers.snowflake.staging.databricks import _qualify_table

        out = _qualify_table(
            {"catalog": "main", "schema": "public"}, "ENCOUNTER",
        )
        assert out == "`main`.`public`.`ENCOUNTER`"

    def test_qualify_table_bare_without_fallbacks_raises(self) -> None:
        from ovid2.drivers.snowflake.staging.databricks import _qualify_table

        with pytest.raises(ValueError, match="no catalog"):
            _qualify_table({"schema": "public"}, "ENCOUNTER")
        with pytest.raises(ValueError, match="no catalog"):
            _qualify_table({"catalog": "main"}, "ENCOUNTER")

    def test_qualify_table_two_parts_without_catalog_raises(self) -> None:
        from ovid2.drivers.snowflake.staging.databricks import _qualify_table

        with pytest.raises(ValueError, match="no catalog"):
            _qualify_table({}, "public.ENCOUNTER")

    def test_qualify_table_escapes_backticks(self) -> None:
        from ovid2.drivers.snowflake.staging.databricks import _qualify_table

        out = _qualify_table({}, "a`b.c.d")
        assert "`a``b`" in out  # doubled backtick

    def test_qualify_table_empty_raises(self) -> None:
        from ovid2.drivers.snowflake.staging.databricks import _qualify_table

        with pytest.raises(ValueError, match="empty table reference"):
            _qualify_table({}, "")

    def test_parse_s3_uri_with_prefix(self) -> None:
        from ovid2.drivers.snowflake.staging.databricks import _parse_s3_uri

        assert _parse_s3_uri("s3://bkt/pfx/") == ("bkt", "pfx")
        assert _parse_s3_uri("s3://bkt/a/b/c/") == ("bkt", "a/b/c")

    def test_parse_s3_uri_bare_bucket(self) -> None:
        from ovid2.drivers.snowflake.staging.databricks import _parse_s3_uri

        assert _parse_s3_uri("s3://bkt") == ("bkt", "")
        assert _parse_s3_uri("s3://bkt/") == ("bkt", "")

    def test_parse_s3_uri_rejects_non_s3(self) -> None:
        from ovid2.drivers.snowflake.staging.databricks import _parse_s3_uri

        with pytest.raises(ValueError, match="not an s3:// URL"):
            _parse_s3_uri("azure://acct/container/")

    def test_s3_dir_uri_composition(self) -> None:
        from ovid2.drivers.snowflake.staging.databricks import _s3_dir_uri

        assert _s3_dir_uri("b", "pfx") == "s3://b/pfx/"
        assert _s3_dir_uri("b", "") == "s3://b/"

    def test_escape_sql(self) -> None:
        from ovid2.drivers.snowflake.staging.databricks import _escape_sql

        assert _escape_sql("O'Brien") == "O''Brien"
        assert _escape_sql("a\\b") == "a\\\\b"

    def test_escape_backtick(self) -> None:
        from ovid2.drivers.snowflake.staging.databricks import _escape_backtick

        assert _escape_backtick("a`b") == "a``b"
        assert _escape_backtick("plain") == "plain"

    def test_rebrand_stage_name(self) -> None:
        from ovid2.drivers.snowflake.staging.databricks import (
            _rebrand_stage_name,
        )

        assert _rebrand_stage_name("OVID2_STG_S3_IN_ABC") == (
            "OVID2_STG_DBX_IN_ABC"
        )
        # No-op when the prefix doesn't match.
        assert _rebrand_stage_name("OTHER") == "OTHER"

    def test_rebrand_temp_table_fqtn(self) -> None:
        from ovid2.drivers.snowflake.staging.databricks import (
            _rebrand_temp_table_fqtn,
        )

        assert _rebrand_temp_table_fqtn("DB.SCH.OVID2_STG_S3_XY") == (
            "DB.SCH.OVID2_STG_DBX_XY"
        )


# ---------------------------------------------------------------------------
# _resolve_bounce_target
# ---------------------------------------------------------------------------


class TestResolveBounceTarget:
    def test_returns_bucket_and_scoped_prefix(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.databricks import (
            _resolve_bounce_target,
        )

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_DATABRICKS_BOUNCE_URI",
            "s3://bouncebkt/dbx/",
        )
        bucket, prefix = _resolve_bounce_target(
            "20260806-140000-def456", "demo", "20260101", "encounter",
            kind="in",
        )
        assert bucket == "bouncebkt"
        assert prefix.startswith("dbx/20260806-140000-def456/demo/20260101/")
        # The suffix is safe-charset only.
        import re
        suffix = prefix.rsplit("/", 1)[-1]
        assert re.match(r"^[A-Za-z0-9_]+$", suffix)
        assert "in" in suffix.lower()

    def test_kind_appears_in_prefix(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.databricks import (
            _resolve_bounce_target,
        )

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_DATABRICKS_BOUNCE_URI",
            "s3://bkt/",
        )
        _, in_prefix = _resolve_bounce_target(
            "i", "p", "b", "t", kind="in",
        )
        _, out_prefix = _resolve_bounce_target(
            "i", "p", "b", "t", kind="out",
        )
        assert in_prefix != out_prefix
        assert in_prefix.endswith("_in")
        assert out_prefix.endswith("_out")

    def test_raises_when_uri_unset(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.databricks import (
            _resolve_bounce_target,
        )

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_DATABRICKS_BOUNCE_URI", None,
        )
        with pytest.raises(ValueError, match="no bounce bucket configured"):
            _resolve_bounce_target("i", "p", "b", "t", kind="in")

    def test_raises_for_non_s3_uri(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.databricks import (
            _resolve_bounce_target,
        )

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_DATABRICKS_BOUNCE_URI",
            "azure://acct/container/",
        )
        with pytest.raises(ValueError, match="not an s3:// URL"):
            _resolve_bounce_target("i", "p", "b", "t", kind="in")


# ---------------------------------------------------------------------------
# _bounce_s3_merged_config
# ---------------------------------------------------------------------------


class TestBounceS3MergedConfig:
    def test_uses_inline_bounce_creds(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.databricks import (
            _bounce_s3_merged_config,
        )

        monkeypatch.setattr(
            settings,
            "SNOWPARK_STAGING_DATABRICKS_BOUNCE_AWS_ACCESS_KEY_ID",
            "AKIA",
        )
        monkeypatch.setattr(
            settings,
            "SNOWPARK_STAGING_DATABRICKS_BOUNCE_AWS_SECRET_ACCESS_KEY",
            "SEC",
        )
        monkeypatch.setattr(
            settings,
            "SNOWPARK_STAGING_DATABRICKS_BOUNCE_AWS_SESSION_TOKEN",
            "TOK",
        )
        monkeypatch.setattr(
            settings,
            "SNOWPARK_STAGING_DATABRICKS_BOUNCE_REGION",
            "us-east-2",
        )
        out = _bounce_s3_merged_config({})
        assert out["aws_access_key_id"] == "AKIA"
        assert out["aws_secret_access_key"] == "SEC"
        assert out["aws_session_token"] == "TOK"
        assert out["region"] == "us-east-2"
        assert out["format"] == "csv"

    def test_no_creds_leaves_config_empty_for_default_provider(
        self, monkeypatch,
    ) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.databricks import (
            _bounce_s3_merged_config,
        )

        for name in (
            "SNOWPARK_STAGING_DATABRICKS_BOUNCE_AWS_ACCESS_KEY_ID",
            "SNOWPARK_STAGING_DATABRICKS_BOUNCE_AWS_SECRET_ACCESS_KEY",
            "SNOWPARK_STAGING_DATABRICKS_BOUNCE_AWS_SESSION_TOKEN",
            "SNOWPARK_STAGING_DATABRICKS_BOUNCE_REGION",
        ):
            monkeypatch.setattr(settings, name, None)
        out = _bounce_s3_merged_config({})
        assert "aws_access_key_id" not in out
        assert "aws_secret_access_key" not in out
        assert "region" not in out
        assert out["format"] == "csv"

    def test_missing_secret_alone_drops_both(self, monkeypatch) -> None:
        """Half-set credentials do NOT partially populate the merged dict."""
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.databricks import (
            _bounce_s3_merged_config,
        )

        monkeypatch.setattr(
            settings,
            "SNOWPARK_STAGING_DATABRICKS_BOUNCE_AWS_ACCESS_KEY_ID",
            "AKIA",
        )
        monkeypatch.setattr(
            settings,
            "SNOWPARK_STAGING_DATABRICKS_BOUNCE_AWS_SECRET_ACCESS_KEY",
            None,
        )
        out = _bounce_s3_merged_config({})
        assert "aws_access_key_id" not in out


# ---------------------------------------------------------------------------
# Databricks-side SQL builders
# ---------------------------------------------------------------------------


class TestDatabricksSql:
    def test_unload_sql_shape(self) -> None:
        from ovid2.drivers.snowflake.staging.databricks import (
            _databricks_unload_sql,
        )

        sql = _databricks_unload_sql(
            source_fqtn="`main`.`public`.`ENC`",
            bounce_uri="s3://b/pfx/",
            field_delimiter=",",
        )
        assert sql.startswith("INSERT OVERWRITE DIRECTORY 's3://b/pfx/'")
        assert "USING csv" in sql
        assert "'header' = 'true'" in sql
        assert "'delimiter' = ','" in sql
        assert "SELECT * FROM `main`.`public`.`ENC`" in sql

    def test_load_sql_shape(self) -> None:
        from ovid2.drivers.snowflake.staging.databricks import (
            _databricks_load_sql,
        )

        sql = _databricks_load_sql(
            target_fqtn="`main`.`public`.`ENC`",
            bounce_uri="s3://b/pfx/",
            field_delimiter="\t",
        )
        assert sql.startswith("COPY INTO `main`.`public`.`ENC`")
        assert "FROM 's3://b/pfx/'" in sql
        assert "FILEFORMAT = CSV" in sql
        assert "'header' = 'true'" in sql
        assert "'delimiter' = '\t'" in sql
        assert "'mergeSchema' = 'true'" in sql
        assert "'force' = 'true'" in sql

    def test_sql_escapes_quotes_in_uri(self) -> None:
        from ovid2.drivers.snowflake.staging.databricks import (
            _databricks_unload_sql,
        )

        sql = _databricks_unload_sql(
            source_fqtn="`a`.`b`.`c`",
            bounce_uri="s3://b/a'b/",
            field_delimiter=",",
        )
        assert "s3://b/a''b/" in sql


# ---------------------------------------------------------------------------
# _run_databricks_sql
# ---------------------------------------------------------------------------


class TestRunDatabricksSql:
    def test_missing_credentials_raises(self) -> None:
        from ovid2.drivers.snowflake.staging.databricks import (
            _run_databricks_sql,
        )

        with pytest.raises(ValueError, match="missing one of host"):
            _run_databricks_sql({"host": "h", "http_path": "p"}, "SELECT 1")

    def test_missing_connector_raises_runtime(self, monkeypatch) -> None:
        from ovid2.drivers.snowflake.staging.databricks import (
            _run_databricks_sql,
        )

        # Make the import fail by removing any cached module.
        monkeypatch.setitem(sys.modules, "databricks", MagicMock(sql=None))

        class _NoSql:
            def __getattr__(self, name: str) -> Any:
                raise ImportError("no databricks.sql")

        # Force `from databricks import sql` to fail.
        fake_pkg = MagicMock()
        del fake_pkg.sql  # ensure attribute lookup raises AttributeError

        # We need `from databricks import sql` to raise ImportError.
        # The cleanest way: install a stub `databricks` module without
        # a `sql` submodule and pre-import guard.
        monkeypatch.setitem(sys.modules, "databricks", MagicMock())

        # Then remove the potential sub-module cache entry.
        monkeypatch.delitem(sys.modules, "databricks.sql", raising=False)

        # Now the `from databricks import sql` call inside the function
        # will try to access `databricks.sql`. Our MagicMock returns a
        # MagicMock for any attribute — that would NOT raise. So the
        # test-in-fact exercised path: swap the whole `databricks`
        # package for one that raises AttributeError on `.sql`.
        class _AttrError:
            def __getattr__(self, name: str) -> Any:
                raise AttributeError(name)

        monkeypatch.setitem(sys.modules, "databricks", _AttrError())

        with pytest.raises((RuntimeError, ImportError, AttributeError)):
            _run_databricks_sql(
                {"host": "h", "http_path": "p", "token": "t"},
                "SELECT 1",
            )

    def test_happy_path_calls_execute(self, monkeypatch) -> None:
        from ovid2.drivers.snowflake.staging.databricks import (
            _run_databricks_sql,
        )

        # Build a fake ``databricks.sql`` module.
        fake_cursor = MagicMock()
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        fake_sql_mod = MagicMock()
        fake_sql_mod.connect.return_value = fake_conn
        fake_dbx = MagicMock()
        fake_dbx.sql = fake_sql_mod
        monkeypatch.setitem(sys.modules, "databricks", fake_dbx)
        monkeypatch.setitem(sys.modules, "databricks.sql", fake_sql_mod)

        _run_databricks_sql(
            {
                "host": "https://ws.example.databricks.com/",
                "http_path": "/sql/1.0/warehouses/abc",
                "token": "dapiXYZ",
            },
            "COPY INTO t FROM 's3://b/'",
        )

        fake_sql_mod.connect.assert_called_once_with(
            server_hostname="ws.example.databricks.com",
            http_path="/sql/1.0/warehouses/abc",
            access_token="dapiXYZ",
        )
        fake_cursor.execute.assert_called_once_with(
            "COPY INTO t FROM 's3://b/'",
        )
        fake_cursor.close.assert_called_once()
        fake_conn.close.assert_called_once()

    def test_closes_connection_even_on_execute_failure(
        self, monkeypatch,
    ) -> None:
        from ovid2.drivers.snowflake.staging.databricks import (
            _run_databricks_sql,
        )

        fake_cursor = MagicMock()
        fake_cursor.execute.side_effect = RuntimeError("boom")
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        fake_sql_mod = MagicMock()
        fake_sql_mod.connect.return_value = fake_conn
        fake_dbx = MagicMock()
        fake_dbx.sql = fake_sql_mod
        monkeypatch.setitem(sys.modules, "databricks", fake_dbx)
        monkeypatch.setitem(sys.modules, "databricks.sql", fake_sql_mod)

        with pytest.raises(RuntimeError, match="boom"):
            _run_databricks_sql(
                {"host": "h", "http_path": "p", "token": "t"},
                "SELECT 1",
            )
        # Both close() calls must have happened.
        fake_cursor.close.assert_called_once()
        fake_conn.close.assert_called_once()


# ---------------------------------------------------------------------------
# materialise — happy path with stubbed boto3 + databricks.sql
# ---------------------------------------------------------------------------


class TestMaterialise:
    def _prepare_stubs(
        self, monkeypatch, *, header: bytes, part_key: str,
    ) -> tuple[MagicMock, MagicMock]:
        """Install fake boto3 + databricks.sql modules."""
        # boto3: get_object returns header bytes; list_objects_v2
        # returns the part-* key.
        fake_client = MagicMock()
        fake_client.get_object.return_value = {
            "Body": MagicMock(read=lambda: header),
        }
        fake_client.list_objects_v2.return_value = {
            "Contents": [
                {"Key": part_key + "/_SUCCESS"},
                {"Key": part_key + "/part-00000-abc.csv"},
            ],
        }
        fake_boto3 = MagicMock()
        fake_boto3.client.return_value = fake_client
        fake_botocore = MagicMock()
        fake_botocore.config = MagicMock()
        fake_botocore.config.Config = MagicMock(return_value="cfg")
        monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
        monkeypatch.setitem(sys.modules, "botocore", fake_botocore)
        monkeypatch.setitem(sys.modules, "botocore.config", fake_botocore.config)

        # databricks.sql
        fake_cursor = MagicMock()
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        fake_sql_mod = MagicMock()
        fake_sql_mod.connect.return_value = fake_conn
        fake_dbx = MagicMock()
        fake_dbx.sql = fake_sql_mod
        monkeypatch.setitem(sys.modules, "databricks", fake_dbx)
        monkeypatch.setitem(sys.modules, "databricks.sql", fake_sql_mod)

        return fake_client, fake_cursor

    def test_materialise_runs_unload_and_snowflake_stage(
        self, monkeypatch,
    ) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.databricks import (
            SnowparkDatabricksStrategy,
        )

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_DATABRICKS_BOUNCE_URI",
            "s3://bouncebkt/dbx/",
        )
        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_S3_INTEGRATION", "MY_INT",
        )
        monkeypatch.setattr(
            settings,
            "SNOWPARK_STAGING_DATABRICKS_BOUNCE_AWS_ACCESS_KEY_ID",
            None,
        )
        monkeypatch.setattr(
            settings,
            "SNOWPARK_STAGING_DATABRICKS_BOUNCE_AWS_SECRET_ACCESS_KEY",
            None,
        )
        _client, dbx_cursor = self._prepare_stubs(
            monkeypatch,
            header=b"COL_A\tCOL_B\nx\ty\n",
            part_key=(
                "dbx/20260806-140000-def456/demo/20260101/"
                "20260806_140000_def456_demo_20260101_encounter_in"
            ),
        )

        session = _FakeSession()
        driver = _FakeDatabricksDriver({
            "host": "https://ws.example.databricks.com/",
            "http_path": "/sql/warehouses/wh1",
            "token": "dapiXYZ",
            "catalog": "main",
            "schema": "public",
        })
        strategy = SnowparkDatabricksStrategy()

        df = strategy.materialise(
            session, driver, "demo", "20260101",
            "ENCOUNTER", None, None,
        )

        # Databricks-side unload fired.
        dbx_cursor.execute.assert_called()
        (unload_sql,), _ = dbx_cursor.execute.call_args
        assert unload_sql.startswith("INSERT OVERWRITE DIRECTORY")
        assert "`main`.`public`.`ENCOUNTER`" in unload_sql

        # Snowflake-side firings.
        create_stage = [
            q for q in session.sql_calls
            if q.startswith("CREATE OR REPLACE TEMPORARY STAGE")
        ]
        create_table = [
            q for q in session.sql_calls
            if q.startswith("CREATE OR REPLACE TRANSIENT TABLE")
        ]
        copy_into = [q for q in session.sql_calls if q.startswith("COPY INTO")]
        assert len(create_stage) == 1
        assert "STORAGE_INTEGRATION = MY_INT" in create_stage[0]
        # Stage name uses the OVID2_STG_DBX_ prefix.
        assert "OVID2_STG_DBX_" in create_stage[0]
        assert len(create_table) == 1
        assert '"COL_A" VARCHAR' in create_table[0]
        assert '"COL_B" VARCHAR' in create_table[0]
        assert len(copy_into) == 1
        # COPY INTO narrows to the discovered part file.
        assert "part-00000-abc.csv" in copy_into[0]
        assert df is not None

    def test_materialise_missing_bounce_uri_raises(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.databricks import (
            SnowparkDatabricksStrategy,
        )

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_DATABRICKS_BOUNCE_URI", None,
        )
        driver = _FakeDatabricksDriver({
            "host": "h", "http_path": "p", "token": "t",
            "catalog": "main", "schema": "public",
        })
        strategy = SnowparkDatabricksStrategy()
        with pytest.raises(ValueError, match="no bounce bucket configured"):
            strategy.materialise(
                _FakeSession(), driver, "demo", "20260101",
                "ENCOUNTER", None, None,
            )

    def test_materialise_raises_when_no_part_file_found(
        self, monkeypatch,
    ) -> None:
        """The bounce listing must find at least one part-*.csv."""
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.databricks import (
            SnowparkDatabricksStrategy,
        )

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_DATABRICKS_BOUNCE_URI",
            "s3://b/",
        )
        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_S3_INTEGRATION", "MY_INT",
        )
        # boto3: list returns only _SUCCESS, no part-*.csv.
        fake_client = MagicMock()
        fake_client.list_objects_v2.return_value = {
            "Contents": [{"Key": "b/_SUCCESS"}],
        }
        fake_boto3 = MagicMock()
        fake_boto3.client.return_value = fake_client
        monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

        # databricks.sql: unload succeeds.
        fake_cursor = MagicMock()
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        fake_sql_mod = MagicMock()
        fake_sql_mod.connect.return_value = fake_conn
        fake_dbx = MagicMock()
        fake_dbx.sql = fake_sql_mod
        monkeypatch.setitem(sys.modules, "databricks", fake_dbx)
        monkeypatch.setitem(sys.modules, "databricks.sql", fake_sql_mod)

        driver = _FakeDatabricksDriver({
            "host": "h", "http_path": "p", "token": "t",
            "catalog": "main", "schema": "public",
        })
        strategy = SnowparkDatabricksStrategy()
        with pytest.raises(ValueError, match="no part-\\*.csv found"):
            strategy.materialise(
                _FakeSession(), driver, "demo", "20260101",
                "ENCOUNTER", None, None,
            )


# ---------------------------------------------------------------------------
# persist — writes to bounce then COPY INTO on Databricks side
# ---------------------------------------------------------------------------


class TestPersist:
    def test_persist_writes_to_bounce_and_runs_copy_into(
        self, monkeypatch,
    ) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.databricks import (
            SnowparkDatabricksStrategy,
        )

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_DATABRICKS_BOUNCE_URI",
            "s3://bouncebkt/dbx/",
        )
        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_S3_INTEGRATION", "MY_INT",
        )

        # Fake databricks.sql
        fake_cursor = MagicMock()
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        fake_sql_mod = MagicMock()
        fake_sql_mod.connect.return_value = fake_conn
        fake_dbx = MagicMock()
        fake_dbx.sql = fake_sql_mod
        monkeypatch.setitem(sys.modules, "databricks", fake_dbx)
        monkeypatch.setitem(sys.modules, "databricks.sql", fake_sql_mod)

        session = _FakeSession()
        driver = _FakeDatabricksDriver({
            "host": "ws.example.databricks.com",
            "http_path": "/sql/1.0/warehouses/w1",
            "token": "dapiXYZ",
            "catalog": "main",
            "schema": "public",
        })
        strategy = SnowparkDatabricksStrategy()
        df = MagicMock()

        strategy.persist(
            session, df, driver, "demo", "20260101", "ENCOUNTER", None,
        )

        # 1) Snowflake CREATE STAGE fired.
        create_stage = [
            q for q in session.sql_calls
            if q.startswith("CREATE OR REPLACE TEMPORARY STAGE")
        ]
        assert len(create_stage) == 1
        assert "OVID2_STG_DBX_" in create_stage[0]
        assert "STORAGE_INTEGRATION = MY_INT" in create_stage[0]

        # 2) copy_into_location on the DataFrame writer.
        df.write.copy_into_location.assert_called_once()
        args_, kwargs_ = df.write.copy_into_location.call_args
        assert args_[0].startswith("@")
        assert kwargs_["file_format_type"] == "csv"
        assert kwargs_["header"] is True

        # 3) Databricks COPY INTO fired.
        fake_cursor.execute.assert_called_once()
        (load_sql,), _ = fake_cursor.execute.call_args
        assert load_sql.startswith("COPY INTO `main`.`public`.`ENCOUNTER`")
        assert "s3://bouncebkt/dbx/" in load_sql
        assert "FILEFORMAT = CSV" in load_sql

    def test_persist_missing_catalog_raises(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.databricks import (
            SnowparkDatabricksStrategy,
        )

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_DATABRICKS_BOUNCE_URI",
            "s3://b/",
        )
        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_S3_INTEGRATION", "INT",
        )
        driver = _FakeDatabricksDriver({
            "host": "h", "http_path": "p", "token": "t",
            # No catalog / schema.
        })
        strategy = SnowparkDatabricksStrategy()
        with pytest.raises(ValueError, match="no catalog"):
            strategy.persist(
                _FakeSession(), MagicMock(), driver,
                "demo", "20260101", "ENCOUNTER", None,
            )

    def test_persist_accepts_dotted_table_reference(
        self, monkeypatch,
    ) -> None:
        """A dotted ``catalog.schema.table`` overrides the driver's."""
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.databricks import (
            SnowparkDatabricksStrategy,
        )

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_DATABRICKS_BOUNCE_URI",
            "s3://b/",
        )
        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_S3_INTEGRATION", "INT",
        )

        fake_cursor = MagicMock()
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        fake_sql_mod = MagicMock()
        fake_sql_mod.connect.return_value = fake_conn
        fake_dbx = MagicMock()
        fake_dbx.sql = fake_sql_mod
        monkeypatch.setitem(sys.modules, "databricks", fake_dbx)
        monkeypatch.setitem(sys.modules, "databricks.sql", fake_sql_mod)

        driver = _FakeDatabricksDriver({
            "host": "h", "http_path": "p", "token": "t",
        })  # no catalog/schema on the driver
        strategy = SnowparkDatabricksStrategy()
        df = MagicMock()

        strategy.persist(
            _FakeSession(), df, driver, "demo", "20260101",
            "othercat.otherschema.T", None,
        )
        (load_sql,), _ = fake_cursor.execute.call_args
        assert "`othercat`.`otherschema`.`T`" in load_sql


# ---------------------------------------------------------------------------
# Cleanup / atexit bookkeeping
# ---------------------------------------------------------------------------


class TestCleanup:
    def test_cleanup_registered_once_per_session_instance(
        self, monkeypatch,
    ) -> None:
        from ovid2.drivers.snowflake.staging.databricks import (
            SnowparkDatabricksStrategy,
        )

        atexit_hooks: list = []
        monkeypatch.setattr(
            "atexit.register", lambda fn: atexit_hooks.append(fn),
        )
        session = _FakeSession()
        strategy = SnowparkDatabricksStrategy()
        strategy._maybe_schedule_cleanup(session, "inst-1")
        strategy._maybe_schedule_cleanup(session, "inst-1")
        strategy._maybe_schedule_cleanup(session, "inst-2")
        assert len(atexit_hooks) == 2

    def test_cleanup_skipped_when_keep_flag_set(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.databricks import (
            SnowparkDatabricksStrategy,
        )

        atexit_hooks: list = []
        monkeypatch.setattr(
            "atexit.register", lambda fn: atexit_hooks.append(fn),
        )
        monkeypatch.setattr(settings, "SNOWPARK_STAGING_KEEP", True)
        session = _FakeSession()
        strategy = SnowparkDatabricksStrategy()
        strategy._maybe_schedule_cleanup(session, "inst-1")
        assert atexit_hooks == []

    def test_cleanup_deletes_bounce_prefix_via_boto3(
        self, monkeypatch,
    ) -> None:
        from ovid2.drivers.snowflake.staging.databricks import (
            SnowparkDatabricksStrategy,
        )

        atexit_hooks: list = []
        monkeypatch.setattr(
            "atexit.register", lambda fn: atexit_hooks.append(fn),
        )

        fake_client = MagicMock()
        # Paginator yields one page with two keys under the prefix.
        page = {
            "Contents": [
                {"Key": "dbx/inst/demo/b/x_in/part-0.csv"},
                {"Key": "dbx/inst/demo/b/x_in/_SUCCESS"},
            ],
        }
        fake_paginator = MagicMock()
        fake_paginator.paginate.return_value = [page]
        fake_client.get_paginator.return_value = fake_paginator
        fake_boto3 = MagicMock()
        fake_boto3.client.return_value = fake_client
        monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

        session = _FakeSession()
        strategy = SnowparkDatabricksStrategy()
        strategy._maybe_schedule_cleanup(session, "inst-1")
        strategy._register_bounce_prefix(
            session, "bouncebkt", "dbx/inst/demo/b/x_in",
        )
        atexit_hooks[0]()  # trigger

        fake_client.delete_objects.assert_called_once()
        _, kwargs = fake_client.delete_objects.call_args
        assert kwargs["Bucket"] == "bouncebkt"
        assert kwargs["Delete"]["Objects"] == [
            {"Key": "dbx/inst/demo/b/x_in/part-0.csv"},
            {"Key": "dbx/inst/demo/b/x_in/_SUCCESS"},
        ]

    def test_cleanup_swallows_boto3_failures(self, monkeypatch) -> None:
        """The atexit hook must never raise."""
        from ovid2.drivers.snowflake.staging.databricks import (
            SnowparkDatabricksStrategy,
        )

        atexit_hooks: list = []
        monkeypatch.setattr(
            "atexit.register", lambda fn: atexit_hooks.append(fn),
        )

        fake_client = MagicMock()
        fake_client.get_paginator.side_effect = RuntimeError("boom")
        fake_boto3 = MagicMock()
        fake_boto3.client.return_value = fake_client
        monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

        session = _FakeSession()
        strategy = SnowparkDatabricksStrategy()
        strategy._maybe_schedule_cleanup(session, "inst-1")
        strategy._register_bounce_prefix(session, "b", "p")
        # Must NOT raise even though get_paginator explodes.
        atexit_hooks[0]()

    def test_cleanup_noop_when_no_objects(self, monkeypatch) -> None:
        from ovid2.drivers.snowflake.staging.databricks import (
            _remove_bounce_prefix,
        )

        fake_client = MagicMock()
        fake_paginator = MagicMock()
        fake_paginator.paginate.return_value = [{"Contents": []}]
        fake_client.get_paginator.return_value = fake_paginator
        fake_boto3 = MagicMock()
        fake_boto3.client.return_value = fake_client
        monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

        _remove_bounce_prefix("b", "p")
        fake_client.delete_objects.assert_not_called()
