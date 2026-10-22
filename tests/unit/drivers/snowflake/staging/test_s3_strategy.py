"""Unit tests for :class:`ovid2.drivers.snowflake.staging.s3.SnowparkS3Strategy`.

Every test drives the strategy through fake Snowpark session doubles
so the suite runs without live Snowflake / AWS credentials.
End-to-end verification against real Snowflake + S3 is documented
in the CHANGELOG entry for Phase D.
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


@pytest.fixture(autouse=True)
def _clear_strategy_state():
    """Reset the strategy's class-level cleanup registries."""
    from ovid2.drivers.snowflake.staging.s3 import SnowparkS3Strategy

    SnowparkS3Strategy._cleanups_scheduled.clear()
    SnowparkS3Strategy._stages.clear()
    SnowparkS3Strategy._temp_tables.clear()
    yield
    SnowparkS3Strategy._cleanups_scheduled.clear()
    SnowparkS3Strategy._stages.clear()
    SnowparkS3Strategy._temp_tables.clear()


class _FakeSession:
    """A Snowpark session double capturing every side effect."""

    def __init__(self) -> None:
        self.sql_calls: list[str] = []
        self.tables_returned: dict[str, Any] = {}
        self.writes: list[Any] = []

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
        return None  # force _ensure_writable_staging_schema to CREATE

    @property
    def native(self) -> Any:
        return self


class _FakeS3Driver:
    """Stand-in for :class:`S3Driver` returning a fixed merged config."""

    type_name = "s3"

    def __init__(self, merged: dict[str, Any]) -> None:
        self._merged = merged

    def as_legacy_src_args(self) -> tuple[str, dict[str, Any]]:
        return "s3", dict(self._merged)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_strategy_registered_under_s3(self) -> None:
        import ovid2.drivers.snowflake  # noqa: F401

        assert "s3" in SnowparkEngineStager._STRATEGIES
        assert SnowparkEngineStager.supports("s3") is True

    def test_registered_class_is_a_snowpark_strategy(self) -> None:
        import ovid2.drivers.snowflake  # noqa: F401

        cls = SnowparkEngineStager._STRATEGIES["s3"]
        assert issubclass(cls, SnowparkStagingStrategy)
        assert cls.driver_type == "s3"


# ---------------------------------------------------------------------------
# Import-safety
# ---------------------------------------------------------------------------


class TestImportSafety:
    def test_module_scope_does_not_import_boto3(self) -> None:
        # Fresh import.
        for mod in list(sys.modules):
            if mod.startswith("boto3") or mod.startswith("botocore"):
                del sys.modules[mod]
        for mod in list(sys.modules):
            if mod == "ovid2.drivers.snowflake.staging.s3":
                del sys.modules[mod]
        import ovid2.drivers.snowflake.staging.s3  # noqa: F401

        for m in sys.modules:
            assert not m.startswith("boto3"), f"boto3 leaked at module scope: {m}"

    def test_module_scope_does_not_import_pyspark(self) -> None:
        for mod in list(sys.modules):
            if mod == "ovid2.drivers.snowflake.staging.s3":
                del sys.modules[mod]
        pyspark_before = {m for m in sys.modules if m.startswith("pyspark")}
        import ovid2.drivers.snowflake.staging.s3  # noqa: F401

        pyspark_after = {m for m in sys.modules if m.startswith("pyspark")}
        assert pyspark_after == pyspark_before


# ---------------------------------------------------------------------------
# _build_create_stage_sql — auth path resolution
# ---------------------------------------------------------------------------


class TestBuildCreateStageSql:
    def test_prefers_storage_integration_when_set(self, monkeypatch) -> None:
        from ovid2.drivers.snowflake.staging import s3

        monkeypatch.setattr(
            s3,
            "SNOWPARK_STAGING_S3_INTEGRATION",
            None,
            raising=False,
        )
        # Direct set via the settings module (Phase D added this).
        from ovid2.config import settings

        monkeypatch.setattr(
            settings,
            "SNOWPARK_STAGING_S3_INTEGRATION",
            "MY_INT",
        )
        sql = s3._build_create_stage_sql(
            stage_name="OVID2_STG_S3_TEST",
            url="s3://bucket/prefix/",
            merged_config={
                "aws_access_key_id": "AKIA",
                "aws_secret_access_key": "secret",
            },
        )
        assert "STORAGE_INTEGRATION = MY_INT" in sql
        assert "AWS_KEY_ID" not in sql  # integration wins

    def test_falls_back_to_static_credentials(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging import s3

        monkeypatch.setattr(
            settings,
            "SNOWPARK_STAGING_S3_INTEGRATION",
            None,
        )
        sql = s3._build_create_stage_sql(
            stage_name="OVID2_STG_S3_TEST",
            url="s3://bucket/prefix/",
            merged_config={
                "aws_access_key_id": "AKIA",
                "aws_secret_access_key": "secret",
            },
        )
        assert "AWS_KEY_ID = 'AKIA'" in sql
        assert "AWS_SECRET_KEY = 'secret'" in sql
        assert "AWS_TOKEN" not in sql

    def test_includes_session_token_when_present(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging import s3

        monkeypatch.setattr(
            settings,
            "SNOWPARK_STAGING_S3_INTEGRATION",
            None,
        )
        sql = s3._build_create_stage_sql(
            stage_name="OVID2_STG_S3_TEST",
            url="s3://bucket/prefix/",
            merged_config={
                "aws_access_key_id": "AKIA",
                "aws_secret_access_key": "secret",
                "aws_session_token": "TOKEN",
            },
        )
        assert "AWS_TOKEN = 'TOKEN'" in sql

    def test_raises_without_any_auth_path(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging import s3

        monkeypatch.setattr(
            settings,
            "SNOWPARK_STAGING_S3_INTEGRATION",
            None,
        )
        with pytest.raises(ValueError, match="no Snowflake→S3 auth path"):
            s3._build_create_stage_sql(
                stage_name="X",
                url="s3://b/",
                merged_config={},
            )

    def test_escapes_special_characters_in_credentials(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging import s3

        monkeypatch.setattr(
            settings,
            "SNOWPARK_STAGING_S3_INTEGRATION",
            None,
        )
        # A key containing a single quote must be escaped.
        sql = s3._build_create_stage_sql(
            stage_name="X",
            url="s3://b/",
            merged_config={
                "aws_access_key_id": "AKI'A",
                "aws_secret_access_key": "s\\ecret",
            },
        )
        assert "AKI''A" in sql
        assert "s\\\\ecret" in sql


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_stage_name_uppercased_and_sanitised(self) -> None:
        from ovid2.drivers.snowflake.staging.s3 import _stage_name

        name = _stage_name(
            "20260806-140000-def456",
            "demo",
            "20260101",
            "encounter",
            kind="in",
        )
        assert name.startswith("OVID2_STG_S3_")
        assert "-" not in name  # sanitised
        assert name == name.upper()

    def test_stage_names_differ_by_kind(self) -> None:
        from ovid2.drivers.snowflake.staging.s3 import _stage_name

        in_name = _stage_name("i", "p", "b", "t", kind="in")
        out_name = _stage_name("i", "p", "b", "t", kind="out")
        assert in_name != out_name
        assert "IN_" in in_name
        assert "OUT_" in out_name

    def test_temp_table_fqtn_sanitised(self) -> None:
        from ovid2.drivers.snowflake.staging.s3 import _temp_table_fqtn

        fqtn = _temp_table_fqtn(
            "TESTDB.OVID2_SNOWPARK_STAGING",
            "20260806-140000-def456",
            "demo",
            "20260101",
            "encounter",
        )
        assert fqtn.startswith("TESTDB.OVID2_SNOWPARK_STAGING.OVID2_STG_S3_")

    def test_s3_dir_uri_composition(self) -> None:
        from ovid2.drivers.snowflake.staging.s3 import _s3_dir_uri

        assert _s3_dir_uri("bucket", "prefix", "demo", "20260101") == (
            "s3://bucket/prefix/demo/20260101/"
        )
        assert _s3_dir_uri("bucket", "", "demo", "20260101") == ("s3://bucket/demo/20260101/")
        assert _s3_dir_uri("bucket", "", "", "") == "s3://bucket/"

    def test_s3_key_composition(self) -> None:
        from ovid2.drivers.snowflake.staging.s3 import _s3_key

        assert _s3_key("prefix", "demo", "20260101", "file.csv") == (
            "prefix/demo/20260101/file.csv"
        )
        assert _s3_key("", "demo", "20260101", "file.csv") == ("demo/20260101/file.csv")

    def test_escape_sql_doubles_single_quotes(self) -> None:
        from ovid2.drivers.snowflake.staging.s3 import _escape_sql

        assert _escape_sql("O'Brien") == "O''Brien"
        assert _escape_sql("path\\file") == "path\\\\file"

    def test_require_csv_raises_for_parquet(self) -> None:
        from ovid2.drivers.snowflake.staging.s3 import _require_csv

        with pytest.raises(ValueError, match="only supports fmt='csv'"):
            _require_csv({"format": "parquet"}, "materialise")

    def test_require_csv_defaults_to_csv(self) -> None:
        from ovid2.drivers.snowflake.staging.s3 import _require_csv

        # No format key → default 'csv' → no raise.
        _require_csv({}, "materialise")

    def test_require_bucket_raises_when_missing(self) -> None:
        from ovid2.drivers.snowflake.staging.s3 import _require_bucket

        with pytest.raises(ValueError, match="no bucket resolved"):
            _require_bucket({})


# ---------------------------------------------------------------------------
# materialise — happy path with a stubbed boto3 header reader
# ---------------------------------------------------------------------------


class TestMaterialise:
    def _prepare_boto_stub(self, monkeypatch, body: bytes) -> MagicMock:
        """Install a fake boto3 module returning ``body`` from get_object."""
        fake_client = MagicMock()
        fake_client.get_object.return_value = {"Body": MagicMock(read=lambda: body)}
        fake_boto3 = MagicMock()
        fake_boto3.client.return_value = fake_client
        fake_botocore = MagicMock()
        fake_botocore.config = MagicMock()
        fake_botocore.config.Config = MagicMock(return_value="cfg-stub")
        monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
        monkeypatch.setitem(sys.modules, "botocore", fake_botocore)
        monkeypatch.setitem(sys.modules, "botocore.config", fake_botocore.config)
        return fake_client

    def test_materialise_creates_stage_and_transient_table(
        self,
        monkeypatch,
    ) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.s3 import SnowparkS3Strategy

        monkeypatch.setattr(
            settings,
            "SNOWPARK_STAGING_S3_INTEGRATION",
            None,
        )
        self._prepare_boto_stub(
            monkeypatch,
            body=b"COL_A\tCOL_B\tCOL_C\nrow1\tx\ty\n",
        )

        session = _FakeSession()
        driver = _FakeS3Driver(
            {
                "bucket": "mybucket",
                "prefix": "raw",
                "region": "us-east-2",
                "aws_access_key_id": "AKIA",
                "aws_secret_access_key": "secret",
                "format": "csv",
            }
        )
        strategy = SnowparkS3Strategy()

        df = strategy.materialise(
            session,
            driver,
            "demo",
            "20260101",
            "encounter.csv",
            None,
            None,
        )

        # A CREATE STAGE, CREATE TRANSIENT TABLE, and COPY INTO fired.
        create_stage = [
            q for q in session.sql_calls if q.startswith("CREATE OR REPLACE TEMPORARY STAGE")
        ]
        create_table = [
            q for q in session.sql_calls if q.startswith("CREATE OR REPLACE TRANSIENT TABLE")
        ]
        copy_into = [q for q in session.sql_calls if q.startswith("COPY INTO")]
        assert len(create_stage) == 1
        assert len(create_table) == 1
        assert len(copy_into) == 1

        # Stage URL points at the directory.
        assert "s3://mybucket/raw/demo/20260101/" in create_stage[0]
        # Column names appear in the transient-table DDL.
        assert '"COL_A" VARCHAR' in create_table[0]
        assert '"COL_C" VARCHAR' in create_table[0]
        # COPY INTO narrows to the specific file.
        assert "encounter.csv" in copy_into[0]
        # DataFrame is the return of session.table(fqtn).
        assert df is not None

    def test_materialise_rejects_non_csv_format(self, monkeypatch) -> None:
        from ovid2.drivers.snowflake.staging.s3 import SnowparkS3Strategy

        session = _FakeSession()
        driver = _FakeS3Driver(
            {
                "bucket": "b",
                "aws_access_key_id": "a",
                "aws_secret_access_key": "s",
                "format": "parquet",
            }
        )
        strategy = SnowparkS3Strategy()
        with pytest.raises(ValueError, match="only supports fmt='csv'"):
            strategy.materialise(
                session,
                driver,
                "demo",
                "20260101",
                "f.parquet",
                None,
                None,
            )

    def test_materialise_rejects_glob_file_name(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.s3 import SnowparkS3Strategy

        monkeypatch.setattr(
            settings,
            "SNOWPARK_STAGING_S3_INTEGRATION",
            None,
        )
        # Even the header-reader guard should catch the glob before
        # boto3 gets called; install a stub anyway so the code path
        # doesn't blow up on a real ``boto3`` import.
        self._prepare_boto_stub(monkeypatch, body=b"a,b\n")

        session = _FakeSession()
        driver = _FakeS3Driver(
            {
                "bucket": "b",
                "aws_access_key_id": "a",
                "aws_secret_access_key": "s",
            }
        )
        strategy = SnowparkS3Strategy()
        with pytest.raises(ValueError, match="cannot read header from a glob"):
            strategy.materialise(
                session,
                driver,
                "demo",
                "20260101",
                "en*.csv",
                None,
                None,
            )


# ---------------------------------------------------------------------------
# persist — writes go directly to S3 (no GET step)
# ---------------------------------------------------------------------------


class TestPersist:
    def test_persist_creates_stage_and_calls_copy_into_location(
        self,
        monkeypatch,
    ) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.s3 import SnowparkS3Strategy

        monkeypatch.setattr(
            settings,
            "SNOWPARK_STAGING_S3_INTEGRATION",
            "MY_INT",
        )
        session = _FakeSession()
        driver = _FakeS3Driver(
            {
                "bucket": "outbucket",
                "prefix": "cooked",
                "format": "csv",
                "single_file": False,
            }
        )
        strategy = SnowparkS3Strategy()

        df = MagicMock()
        strategy.persist(
            session,
            df,
            driver,
            "demo",
            "20260101",
            "encounter",
            None,
        )

        create_stage = [
            q for q in session.sql_calls if q.startswith("CREATE OR REPLACE TEMPORARY STAGE")
        ]
        assert len(create_stage) == 1
        assert "s3://outbucket/cooked/demo/20260101/" in create_stage[0]
        assert "STORAGE_INTEGRATION = MY_INT" in create_stage[0]

        # No GET step — the write is server-side via
        # copy_into_location on the DataFrameWriter.
        assert not any(q.startswith("GET ") for q in session.sql_calls)
        # The DataFrame's writer.copy_into_location was called.
        df.write.copy_into_location.assert_called_once()
        _, kwargs = df.write.copy_into_location.call_args
        # Stage target ends with /<table>/.
        args, _ = df.write.copy_into_location.call_args
        assert args[0].endswith("/encounter/")
        assert kwargs["file_format_type"] == "csv"
        assert kwargs["header"] is True
        assert kwargs["single"] is False  # default from location

    def test_persist_honours_single_file_true(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.s3 import SnowparkS3Strategy

        monkeypatch.setattr(
            settings,
            "SNOWPARK_STAGING_S3_INTEGRATION",
            "INT",
        )
        session = _FakeSession()
        driver = _FakeS3Driver(
            {
                "bucket": "b",
                "single_file": True,
                "format": "csv",
            }
        )
        strategy = SnowparkS3Strategy()
        df = MagicMock()
        strategy.persist(
            session,
            df,
            driver,
            "p",
            "b",
            "t",
            None,
        )
        _, kwargs = df.write.copy_into_location.call_args
        assert kwargs["single"] is True

    def test_persist_rejects_non_csv(self) -> None:
        from ovid2.drivers.snowflake.staging.s3 import SnowparkS3Strategy

        session = _FakeSession()
        driver = _FakeS3Driver(
            {
                "bucket": "b",
                "format": "parquet",
            }
        )
        strategy = SnowparkS3Strategy()
        with pytest.raises(ValueError, match="only supports fmt='csv'"):
            strategy.persist(
                session,
                MagicMock(),
                driver,
                "p",
                "b",
                "t",
                None,
            )


# ---------------------------------------------------------------------------
# Cleanup / atexit bookkeeping
# ---------------------------------------------------------------------------


class TestCleanup:
    def test_cleanup_registered_once_per_session_instance(
        self,
        monkeypatch,
    ) -> None:
        from ovid2.drivers.snowflake.staging.s3 import SnowparkS3Strategy

        atexit_calls: list = []
        monkeypatch.setattr(
            "atexit.register",
            lambda fn: atexit_calls.append(fn),
        )
        session = _FakeSession()
        strategy = SnowparkS3Strategy()
        strategy._maybe_schedule_cleanup(session, "inst-1")
        strategy._maybe_schedule_cleanup(session, "inst-1")
        strategy._maybe_schedule_cleanup(session, "inst-2")
        # Two distinct instances → two atexit hooks; the second
        # inst-1 call is a no-op.
        assert len(atexit_calls) == 2

    def test_cleanup_skipped_when_keep_flag_set(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.s3 import SnowparkS3Strategy

        atexit_calls: list = []
        monkeypatch.setattr(
            "atexit.register",
            lambda fn: atexit_calls.append(fn),
        )
        monkeypatch.setattr(settings, "SNOWPARK_STAGING_KEEP", True)
        session = _FakeSession()
        strategy = SnowparkS3Strategy()
        strategy._maybe_schedule_cleanup(session, "inst-1")
        assert atexit_calls == []

    def test_cleanup_drops_stages_and_tables(self, monkeypatch) -> None:
        from ovid2.drivers.snowflake.staging.s3 import SnowparkS3Strategy

        atexit_hooks: list = []
        monkeypatch.setattr(
            "atexit.register",
            lambda fn: atexit_hooks.append(fn),
        )
        session = _FakeSession()
        strategy = SnowparkS3Strategy()
        strategy._maybe_schedule_cleanup(session, "inst-1")
        strategy._register_stage(session, "OVID2_STG_S3_A")
        strategy._register_temp_table(session, "DB.SCH.OVID2_STG_S3_A")

        # Trigger the registered hook.
        assert len(atexit_hooks) == 1
        atexit_hooks[0]()

        drop_stages = [q for q in session.sql_calls if q.startswith("DROP STAGE")]
        drop_tables = [q for q in session.sql_calls if q.startswith("DROP TABLE")]
        assert "DROP STAGE IF EXISTS OVID2_STG_S3_A" in drop_stages
        assert "DROP TABLE IF EXISTS DB.SCH.OVID2_STG_S3_A" in drop_tables

    def test_cleanup_swallows_drop_failures(self, monkeypatch) -> None:
        """A failed DROP must not raise — the atexit hook must never crash."""
        from ovid2.drivers.snowflake.staging.s3 import SnowparkS3Strategy

        atexit_hooks: list = []
        monkeypatch.setattr(
            "atexit.register",
            lambda fn: atexit_hooks.append(fn),
        )
        session = _FakeSession()

        # Make sql() raise for DROP STAGE.
        real_sql = session.sql

        def bombing_sql(q: str) -> Any:
            if q.startswith("DROP STAGE"):
                raise RuntimeError("boom")
            return real_sql(q)

        session.sql = bombing_sql  # type: ignore[method-assign]

        strategy = SnowparkS3Strategy()
        strategy._maybe_schedule_cleanup(session, "inst-1")
        strategy._register_stage(session, "OVID2_STG_S3_A")
        atexit_hooks[0]()  # must not raise
