"""Unit tests for :class:`ovid2.drivers.snowflake.staging.gcs.SnowparkGcsStrategy`.

Every test drives the strategy through fake Snowpark session doubles
so the suite runs without live Snowflake / GCP credentials.
End-to-end verification against real Snowflake + GCS is documented
in the CHANGELOG entry for Phase F.
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
    monkeypatch.delenv("OVID2_SNOWPARK_STAGING_GCS_INTEGRATION", raising=False)
    monkeypatch.delenv("OVID2_SNOWPARK_STAGING_SCHEMA", raising=False)


@pytest.fixture(autouse=True)
def _clear_strategy_state():
    """Reset the strategy's class-level cleanup registries."""
    from ovid2.drivers.snowflake.staging.gcs import SnowparkGcsStrategy

    SnowparkGcsStrategy._cleanups_scheduled.clear()
    SnowparkGcsStrategy._stages.clear()
    SnowparkGcsStrategy._temp_tables.clear()
    yield
    SnowparkGcsStrategy._cleanups_scheduled.clear()
    SnowparkGcsStrategy._stages.clear()
    SnowparkGcsStrategy._temp_tables.clear()


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
    def native(self) -> "Any":
        return self


class _FakeGcsDriver:
    """Stand-in for the gcs legacy adapter returning a fixed merged config."""

    type_name = "gcs"

    def __init__(self, merged: dict[str, Any]) -> None:
        self._merged = merged

    def as_legacy_src_args(self) -> tuple[str, dict[str, Any]]:
        return "gcs", dict(self._merged)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_strategy_registered_under_gcs(self) -> None:
        import ovid2.drivers.snowflake  # noqa: F401

        assert "gcs" in SnowparkEngineStager._STRATEGIES
        assert SnowparkEngineStager.supports("gcs") is True

    def test_registered_class_is_a_snowpark_strategy(self) -> None:
        import ovid2.drivers.snowflake  # noqa: F401

        cls = SnowparkEngineStager._STRATEGIES["gcs"]
        assert issubclass(cls, SnowparkStagingStrategy)
        assert cls.driver_type == "gcs"


# ---------------------------------------------------------------------------
# Import-safety
# ---------------------------------------------------------------------------


class TestImportSafety:
    def test_module_scope_does_not_import_google_cloud_storage(self) -> None:
        # Fresh import.
        for mod in list(sys.modules):
            if mod.startswith("google.cloud") or mod.startswith("google.oauth2"):
                del sys.modules[mod]
        for mod in list(sys.modules):
            if mod == "ovid2.drivers.snowflake.staging.gcs":
                del sys.modules[mod]
        import ovid2.drivers.snowflake.staging.gcs  # noqa: F401

        for m in sys.modules:
            assert not m.startswith("google.cloud.storage"), (
                f"google.cloud.storage leaked at module scope: {m}"
            )
            assert not m.startswith("google.oauth2"), (
                f"google.oauth2 leaked at module scope: {m}"
            )

    def test_module_scope_does_not_import_pyspark(self) -> None:
        for mod in list(sys.modules):
            if mod == "ovid2.drivers.snowflake.staging.gcs":
                del sys.modules[mod]
        pyspark_before = {m for m in sys.modules if m.startswith("pyspark")}
        import ovid2.drivers.snowflake.staging.gcs  # noqa: F401
        pyspark_after = {m for m in sys.modules if m.startswith("pyspark")}
        assert pyspark_after == pyspark_before

    def test_module_scope_does_not_import_snowpark(self) -> None:
        for mod in list(sys.modules):
            if mod == "ovid2.drivers.snowflake.staging.gcs":
                del sys.modules[mod]
        snowpark_before = {m for m in sys.modules if m.startswith("snowflake.snowpark")}
        import ovid2.drivers.snowflake.staging.gcs  # noqa: F401
        snowpark_after = {m for m in sys.modules if m.startswith("snowflake.snowpark")}
        assert snowpark_after == snowpark_before


# ---------------------------------------------------------------------------
# _build_create_stage_sql — auth path resolution
# ---------------------------------------------------------------------------


class TestBuildCreateStageSql:
    def test_uses_storage_integration_when_set(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging import gcs

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_GCS_INTEGRATION", "MY_GCS_INT",
        )
        sql = gcs._build_create_stage_sql(
            stage_name="OVID2_STG_GCS_TEST",
            url="gcs://bucket/prefix/",
        )
        assert "STORAGE_INTEGRATION = MY_GCS_INT" in sql
        assert "CREATE OR REPLACE TEMPORARY STAGE OVID2_STG_GCS_TEST" in sql
        assert "URL = 'gcs://bucket/prefix/'" in sql

    def test_raises_when_no_integration_configured(self, monkeypatch) -> None:
        """GCS is integration-only — no inline fallback exists."""
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging import gcs

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_GCS_INTEGRATION", None,
        )
        with pytest.raises(ValueError, match="no Snowflake→GCS auth path"):
            gcs._build_create_stage_sql(
                stage_name="X",
                url="gcs://b/",
            )

    def test_error_names_the_env_var(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging import gcs

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_GCS_INTEGRATION", None,
        )
        with pytest.raises(ValueError) as exc_info:
            gcs._build_create_stage_sql(stage_name="X", url="gcs://b/")
        assert "OVID2_SNOWPARK_STAGING_GCS_INTEGRATION" in str(exc_info.value)

    def test_url_is_escaped(self, monkeypatch) -> None:
        """A URL with a single quote is escaped in the DDL."""
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging import gcs

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_GCS_INTEGRATION", "INT",
        )
        # Contrived — the composed URL never contains a quote today,
        # but the escape guard must fire regardless.
        sql = gcs._build_create_stage_sql(
            stage_name="X",
            url="gcs://b/it's/a/prefix/",
        )
        assert "gcs://b/it''s/a/prefix/" in sql


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_stage_name_uppercased_and_sanitised(self) -> None:
        from ovid2.drivers.snowflake.staging.gcs import _stage_name

        name = _stage_name(
            "20260806-140000-def456", "demo", "20260101", "encounter", kind="in",
        )
        assert name.startswith("OVID2_STG_GCS_")
        assert "-" not in name  # sanitised
        assert name == name.upper()

    def test_stage_names_differ_by_kind(self) -> None:
        from ovid2.drivers.snowflake.staging.gcs import _stage_name

        in_name = _stage_name("i", "p", "b", "t", kind="in")
        out_name = _stage_name("i", "p", "b", "t", kind="out")
        assert in_name != out_name
        assert "IN_" in in_name
        assert "OUT_" in out_name

    def test_stage_name_prefix_distinguishes_from_s3(self) -> None:
        """Operators can reap GCS-strategy orphans separately from S3."""
        from ovid2.drivers.snowflake.staging.gcs import _stage_name

        gcs_name = _stage_name("i", "p", "b", "t", kind="in")
        assert gcs_name.startswith("OVID2_STG_GCS_")
        assert "STG_S3_" not in gcs_name

    def test_temp_table_fqtn_sanitised(self) -> None:
        from ovid2.drivers.snowflake.staging.gcs import _temp_table_fqtn

        fqtn = _temp_table_fqtn(
            "TESTDB.OVID2_SNOWPARK_STAGING",
            "20260806-140000-def456",
            "demo",
            "20260101",
            "encounter",
        )
        assert fqtn.startswith("TESTDB.OVID2_SNOWPARK_STAGING.OVID2_STG_GCS_")

    def test_gcs_dir_uri_composition(self) -> None:
        from ovid2.drivers.snowflake.staging.gcs import _gcs_dir_uri

        assert _gcs_dir_uri("bucket", "prefix", "demo", "20260101") == (
            "gcs://bucket/prefix/demo/20260101/"
        )
        assert _gcs_dir_uri("bucket", "", "demo", "20260101") == (
            "gcs://bucket/demo/20260101/"
        )
        assert _gcs_dir_uri("bucket", "", "", "") == "gcs://bucket/"

    def test_gcs_dir_uri_uses_gcs_scheme(self) -> None:
        """Snowflake accepts ``gcs://`` in ``CREATE STAGE URL='…'``.

        Even though the canonical Google scheme is ``gs://``, the
        Snowflake external-stage URL grammar uses ``gcs://``.
        """
        from ovid2.drivers.snowflake.staging.gcs import _gcs_dir_uri

        uri = _gcs_dir_uri("b", "p", "demo", "20260101")
        assert uri.startswith("gcs://")
        assert not uri.startswith("gs://")

    def test_gcs_key_composition(self) -> None:
        from ovid2.drivers.snowflake.staging.gcs import _gcs_key

        assert _gcs_key("prefix", "demo", "20260101", "file.csv") == (
            "prefix/demo/20260101/file.csv"
        )
        assert _gcs_key("", "demo", "20260101", "file.csv") == (
            "demo/20260101/file.csv"
        )

    def test_escape_sql_doubles_single_quotes(self) -> None:
        from ovid2.drivers.snowflake.staging.gcs import _escape_sql

        assert _escape_sql("O'Brien") == "O''Brien"
        assert _escape_sql("path\\file") == "path\\\\file"

    def test_require_csv_raises_for_parquet(self) -> None:
        from ovid2.drivers.snowflake.staging.gcs import _require_csv

        with pytest.raises(ValueError, match="only supports fmt='csv'"):
            _require_csv({"format": "parquet"}, "materialise")

    def test_require_csv_defaults_to_csv(self) -> None:
        from ovid2.drivers.snowflake.staging.gcs import _require_csv

        # No format key → default 'csv' → no raise.
        _require_csv({}, "materialise")

    def test_require_bucket_raises_when_missing(self) -> None:
        from ovid2.drivers.snowflake.staging.gcs import _require_bucket

        with pytest.raises(ValueError, match="no bucket resolved"):
            _require_bucket({})

    def test_merged_config_from_legacy_adapter_shape(self) -> None:
        from ovid2.drivers.snowflake.staging.gcs import _merged_config

        driver = _FakeGcsDriver({"bucket": "b", "prefix": "p"})
        merged = _merged_config(driver)
        assert merged == {"bucket": "b", "prefix": "p"}

    def test_merged_config_falls_back_to_location(self) -> None:
        """A driver without ``as_legacy_src_args`` still yields config."""
        from ovid2.drivers.snowflake.staging.gcs import _merged_config

        class _NoAdapter:
            location = {"bucket": "onlyloc"}

        merged = _merged_config(_NoAdapter())
        assert merged == {"bucket": "onlyloc"}


# ---------------------------------------------------------------------------
# _read_header_columns_from_gcs — local ranged download
# ---------------------------------------------------------------------------


class TestHeaderReader:
    def _prepare_gcs_stub(
        self, monkeypatch, body: bytes,
    ) -> tuple[MagicMock, MagicMock]:
        """Install a fake google-cloud-storage module returning ``body``."""
        fake_blob = MagicMock()
        fake_blob.download_as_bytes.return_value = body
        fake_bucket = MagicMock()
        fake_bucket.blob.return_value = fake_blob
        fake_client = MagicMock()
        fake_client.bucket.return_value = fake_bucket
        fake_storage = MagicMock()
        fake_storage.Client.return_value = fake_client
        fake_cloud = MagicMock()
        fake_cloud.storage = fake_storage
        fake_google = MagicMock()
        fake_google.cloud = fake_cloud
        # google.oauth2.service_account
        fake_sa = MagicMock()
        fake_sa.Credentials.from_service_account_info.return_value = "creds-stub"
        fake_oauth2 = MagicMock()
        fake_oauth2.service_account = fake_sa
        fake_google.oauth2 = fake_oauth2
        monkeypatch.setitem(sys.modules, "google", fake_google)
        monkeypatch.setitem(sys.modules, "google.cloud", fake_cloud)
        monkeypatch.setitem(sys.modules, "google.cloud.storage", fake_storage)
        monkeypatch.setitem(sys.modules, "google.oauth2", fake_oauth2)
        monkeypatch.setitem(
            sys.modules, "google.oauth2.service_account", fake_sa,
        )
        return fake_client, fake_blob

    def test_extracts_columns_from_body(self, monkeypatch) -> None:
        from ovid2.drivers.snowflake.staging.gcs import (
            _read_header_columns_from_gcs,
        )

        self._prepare_gcs_stub(
            monkeypatch, body=b"COL_A,COL_B,COL_C\nrow1,x,y\n",
        )
        cols = _read_header_columns_from_gcs(
            bucket="b", key="demo/20260101/enc.csv",
            delimiter=",", merged_config={"project_id": "proj"},
        )
        assert cols == ["COL_A", "COL_B", "COL_C"]

    def test_extracts_columns_with_tab_delimiter(self, monkeypatch) -> None:
        from ovid2.drivers.snowflake.staging.gcs import (
            _read_header_columns_from_gcs,
        )

        self._prepare_gcs_stub(monkeypatch, body=b"A\tB\tC\nrow\n")
        cols = _read_header_columns_from_gcs(
            bucket="b", key="k", delimiter="\t", merged_config={},
        )
        assert cols == ["A", "B", "C"]

    def test_strips_bom(self, monkeypatch) -> None:
        from ovid2.drivers.snowflake.staging.gcs import (
            _read_header_columns_from_gcs,
        )

        self._prepare_gcs_stub(
            monkeypatch, body="\ufeffCOL_A,COL_B\n".encode("utf-8"),
        )
        cols = _read_header_columns_from_gcs(
            bucket="b", key="k", delimiter=",", merged_config={},
        )
        assert cols == ["COL_A", "COL_B"]

    def test_uses_service_account_json_when_present(self, monkeypatch) -> None:
        from ovid2.drivers.snowflake.staging.gcs import (
            _read_header_columns_from_gcs,
        )

        fake_client, _ = self._prepare_gcs_stub(monkeypatch, body=b"A,B\n")
        _read_header_columns_from_gcs(
            bucket="b", key="k", delimiter=",",
            merged_config={
                "project_id": "proj",
                "service_account_json": '{"type": "service_account"}',
            },
        )
        # storage.Client received credentials via credentials= kwarg.
        import google.cloud.storage as storage  # type: ignore[import]

        _, kwargs = storage.Client.call_args
        assert kwargs["project"] == "proj"
        assert "credentials" in kwargs

    def test_falls_back_to_adc_when_no_service_account(
        self, monkeypatch,
    ) -> None:
        from ovid2.drivers.snowflake.staging.gcs import (
            _read_header_columns_from_gcs,
        )

        self._prepare_gcs_stub(monkeypatch, body=b"A,B\n")
        _read_header_columns_from_gcs(
            bucket="b", key="k", delimiter=",", merged_config={},
        )
        # storage.Client called with no credentials kwarg → ADC path.
        import google.cloud.storage as storage  # type: ignore[import]

        _, kwargs = storage.Client.call_args
        assert "credentials" not in kwargs

    def test_rejects_glob_in_key(self, monkeypatch) -> None:
        from ovid2.drivers.snowflake.staging.gcs import (
            _read_header_columns_from_gcs,
        )

        with pytest.raises(ValueError, match="cannot read header from a glob"):
            _read_header_columns_from_gcs(
                bucket="b", key="path/*.csv",
                delimiter=",", merged_config={},
            )

    def test_raises_on_empty_body(self, monkeypatch) -> None:
        from ovid2.drivers.snowflake.staging.gcs import (
            _read_header_columns_from_gcs,
        )

        self._prepare_gcs_stub(monkeypatch, body=b"")
        with pytest.raises(ValueError, match="is empty"):
            _read_header_columns_from_gcs(
                bucket="b", key="k", delimiter=",", merged_config={},
            )

    def test_service_account_json_can_be_dict(self, monkeypatch) -> None:
        """The connection may hand us a pre-parsed dict."""
        from ovid2.drivers.snowflake.staging.gcs import (
            _read_header_columns_from_gcs,
        )

        self._prepare_gcs_stub(monkeypatch, body=b"A\n")
        _read_header_columns_from_gcs(
            bucket="b", key="k", delimiter=",",
            merged_config={
                "service_account_json": {"type": "service_account"},
            },
        )


# ---------------------------------------------------------------------------
# materialise — happy path with a stubbed header reader
# ---------------------------------------------------------------------------


class TestMaterialise:
    def _prepare_gcs_stub(self, monkeypatch, body: bytes) -> MagicMock:
        """Install a fake google-cloud-storage module returning ``body``."""
        fake_blob = MagicMock()
        fake_blob.download_as_bytes.return_value = body
        fake_bucket = MagicMock()
        fake_bucket.blob.return_value = fake_blob
        fake_client = MagicMock()
        fake_client.bucket.return_value = fake_bucket
        fake_storage = MagicMock()
        fake_storage.Client.return_value = fake_client
        fake_cloud = MagicMock()
        fake_cloud.storage = fake_storage
        fake_google = MagicMock()
        fake_google.cloud = fake_cloud
        fake_sa = MagicMock()
        fake_sa.Credentials.from_service_account_info.return_value = "creds"
        fake_oauth2 = MagicMock()
        fake_oauth2.service_account = fake_sa
        fake_google.oauth2 = fake_oauth2
        monkeypatch.setitem(sys.modules, "google", fake_google)
        monkeypatch.setitem(sys.modules, "google.cloud", fake_cloud)
        monkeypatch.setitem(sys.modules, "google.cloud.storage", fake_storage)
        monkeypatch.setitem(sys.modules, "google.oauth2", fake_oauth2)
        monkeypatch.setitem(
            sys.modules, "google.oauth2.service_account", fake_sa,
        )
        return fake_client

    def test_materialise_creates_stage_and_transient_table(
        self, monkeypatch,
    ) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.gcs import SnowparkGcsStrategy

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_GCS_INTEGRATION", "MY_GCS_INT",
        )
        self._prepare_gcs_stub(
            monkeypatch, body=b"COL_A\tCOL_B\tCOL_C\nrow1\tx\ty\n",
        )

        session = _FakeSession()
        driver = _FakeGcsDriver({
            "bucket": "mybucket",
            "prefix": "raw",
            "project_id": "proj",
            "service_account_json": '{"type": "service_account"}',
            "format": "csv",
        })
        strategy = SnowparkGcsStrategy()

        df = strategy.materialise(
            session, driver, "demo", "20260101",
            "encounter.csv", None, None,
        )

        # A CREATE STAGE, CREATE TRANSIENT TABLE, and COPY INTO fired.
        create_stage = [q for q in session.sql_calls if q.startswith("CREATE OR REPLACE TEMPORARY STAGE")]
        create_table = [q for q in session.sql_calls if q.startswith("CREATE OR REPLACE TRANSIENT TABLE")]
        copy_into = [q for q in session.sql_calls if q.startswith("COPY INTO")]
        assert len(create_stage) == 1
        assert len(create_table) == 1
        assert len(copy_into) == 1

        # Stage URL points at the directory (with gcs:// scheme).
        assert "gcs://mybucket/raw/demo/20260101/" in create_stage[0]
        assert "STORAGE_INTEGRATION = MY_GCS_INT" in create_stage[0]
        # Column names appear in the transient-table DDL.
        assert '"COL_A" VARCHAR' in create_table[0]
        assert '"COL_C" VARCHAR' in create_table[0]
        # COPY INTO narrows to the specific file.
        assert "encounter.csv" in copy_into[0]
        # DataFrame is the return of session.table(fqtn).
        assert df is not None

    def test_materialise_rejects_non_csv_format(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.gcs import SnowparkGcsStrategy

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_GCS_INTEGRATION", "INT",
        )
        session = _FakeSession()
        driver = _FakeGcsDriver({
            "bucket": "b",
            "format": "parquet",
        })
        strategy = SnowparkGcsStrategy()
        with pytest.raises(ValueError, match="only supports fmt='csv'"):
            strategy.materialise(
                session, driver, "demo", "20260101", "f.parquet", None, None,
            )

    def test_materialise_rejects_glob_file_name(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.gcs import SnowparkGcsStrategy

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_GCS_INTEGRATION", "INT",
        )
        # Stub anyway so the reader import doesn't blow up.
        self._prepare_gcs_stub(monkeypatch, body=b"a,b\n")

        session = _FakeSession()
        driver = _FakeGcsDriver({
            "bucket": "b",
        })
        strategy = SnowparkGcsStrategy()
        with pytest.raises(ValueError, match="cannot read header from a glob"):
            strategy.materialise(
                session, driver, "demo", "20260101", "en*.csv", None, None,
            )

    def test_materialise_aborts_before_header_read_when_no_integration(
        self, monkeypatch,
    ) -> None:
        """Missing integration must fail before the local GCS call.

        Otherwise operators without google-cloud-storage installed would
        see a spurious ImportError instead of the plan-time config
        error that names the fix.
        """
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.gcs import SnowparkGcsStrategy

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_GCS_INTEGRATION", None,
        )
        # Deliberately DO NOT stub google-cloud-storage. If the
        # strategy tries to reach it before the integration check
        # fires, this test surfaces as ImportError, not ValueError.
        session = _FakeSession()
        driver = _FakeGcsDriver({"bucket": "b", "format": "csv"})
        strategy = SnowparkGcsStrategy()
        with pytest.raises(ValueError, match="no Snowflake→GCS auth path"):
            strategy.materialise(
                session, driver, "demo", "20260101", "enc.csv", None, None,
            )


# ---------------------------------------------------------------------------
# persist — writes go directly to GCS (no download step)
# ---------------------------------------------------------------------------


class TestPersist:
    def test_persist_creates_stage_and_calls_copy_into_location(
        self, monkeypatch,
    ) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.gcs import SnowparkGcsStrategy

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_GCS_INTEGRATION", "MY_GCS_INT",
        )
        session = _FakeSession()
        driver = _FakeGcsDriver({
            "bucket": "outbucket",
            "prefix": "cooked",
            "format": "csv",
            "single_file": False,
        })
        strategy = SnowparkGcsStrategy()

        df = MagicMock()
        strategy.persist(
            session, df, driver, "demo", "20260101", "encounter", None,
        )

        create_stage = [q for q in session.sql_calls if q.startswith("CREATE OR REPLACE TEMPORARY STAGE")]
        assert len(create_stage) == 1
        assert "gcs://outbucket/cooked/demo/20260101/" in create_stage[0]
        assert "STORAGE_INTEGRATION = MY_GCS_INT" in create_stage[0]

        # No GET step — the write is server-side via
        # copy_into_location on the DataFrameWriter.
        assert not any(q.startswith("GET ") for q in session.sql_calls)
        # The DataFrame's writer.copy_into_location was called.
        df.write.copy_into_location.assert_called_once()
        args, kwargs = df.write.copy_into_location.call_args
        # Stage target ends with /<table>/.
        assert args[0].endswith("/encounter/")
        assert kwargs["file_format_type"] == "csv"
        assert kwargs["header"] is True
        assert kwargs["single"] is False  # default from location

    def test_persist_honours_single_file_true(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.gcs import SnowparkGcsStrategy

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_GCS_INTEGRATION", "INT",
        )
        session = _FakeSession()
        driver = _FakeGcsDriver({
            "bucket": "b",
            "single_file": True,
            "format": "csv",
        })
        strategy = SnowparkGcsStrategy()
        df = MagicMock()
        strategy.persist(
            session, df, driver, "p", "b", "t", None,
        )
        _, kwargs = df.write.copy_into_location.call_args
        assert kwargs["single"] is True

    def test_persist_rejects_non_csv(self) -> None:
        from ovid2.drivers.snowflake.staging.gcs import SnowparkGcsStrategy

        session = _FakeSession()
        driver = _FakeGcsDriver({
            "bucket": "b", "format": "parquet",
        })
        strategy = SnowparkGcsStrategy()
        with pytest.raises(ValueError, match="only supports fmt='csv'"):
            strategy.persist(
                session, MagicMock(), driver, "p", "b", "t", None,
            )

    def test_persist_rejects_missing_bucket(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.gcs import SnowparkGcsStrategy

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_GCS_INTEGRATION", "INT",
        )
        session = _FakeSession()
        driver = _FakeGcsDriver({"format": "csv"})  # no bucket
        strategy = SnowparkGcsStrategy()
        with pytest.raises(ValueError, match="no bucket resolved"):
            strategy.persist(
                session, MagicMock(), driver, "p", "b", "t", None,
            )

    def test_persist_aborts_before_stage_create_when_no_integration(
        self, monkeypatch,
    ) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.gcs import SnowparkGcsStrategy

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_GCS_INTEGRATION", None,
        )
        session = _FakeSession()
        driver = _FakeGcsDriver({"bucket": "b", "format": "csv"})
        strategy = SnowparkGcsStrategy()
        with pytest.raises(ValueError, match="no Snowflake→GCS auth path"):
            strategy.persist(
                session, MagicMock(), driver, "p", "b", "t", None,
            )


# ---------------------------------------------------------------------------
# Cleanup / atexit bookkeeping
# ---------------------------------------------------------------------------


class TestCleanup:
    def test_cleanup_registered_once_per_session_instance(
        self, monkeypatch,
    ) -> None:
        from ovid2.drivers.snowflake.staging.gcs import SnowparkGcsStrategy

        atexit_calls: list = []
        monkeypatch.setattr(
            "atexit.register", lambda fn: atexit_calls.append(fn),
        )
        session = _FakeSession()
        strategy = SnowparkGcsStrategy()
        strategy._maybe_schedule_cleanup(session, "inst-1")
        strategy._maybe_schedule_cleanup(session, "inst-1")
        strategy._maybe_schedule_cleanup(session, "inst-2")
        # Two distinct instances → two atexit hooks; the second
        # inst-1 call is a no-op.
        assert len(atexit_calls) == 2

    def test_cleanup_skipped_when_keep_flag_set(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.gcs import SnowparkGcsStrategy

        atexit_calls: list = []
        monkeypatch.setattr(
            "atexit.register", lambda fn: atexit_calls.append(fn),
        )
        monkeypatch.setattr(settings, "SNOWPARK_STAGING_KEEP", True)
        session = _FakeSession()
        strategy = SnowparkGcsStrategy()
        strategy._maybe_schedule_cleanup(session, "inst-1")
        assert atexit_calls == []

    def test_cleanup_drops_stages_and_tables(self, monkeypatch) -> None:
        from ovid2.drivers.snowflake.staging.gcs import SnowparkGcsStrategy

        atexit_hooks: list = []
        monkeypatch.setattr(
            "atexit.register", lambda fn: atexit_hooks.append(fn),
        )
        session = _FakeSession()
        strategy = SnowparkGcsStrategy()
        strategy._maybe_schedule_cleanup(session, "inst-1")
        strategy._register_stage(session, "OVID2_STG_GCS_A")
        strategy._register_temp_table(session, "DB.SCH.OVID2_STG_GCS_A")

        # Trigger the registered hook.
        assert len(atexit_hooks) == 1
        atexit_hooks[0]()

        drop_stages = [q for q in session.sql_calls if q.startswith("DROP STAGE")]
        drop_tables = [q for q in session.sql_calls if q.startswith("DROP TABLE")]
        assert "DROP STAGE IF EXISTS OVID2_STG_GCS_A" in drop_stages
        assert "DROP TABLE IF EXISTS DB.SCH.OVID2_STG_GCS_A" in drop_tables

    def test_cleanup_swallows_drop_failures(self, monkeypatch) -> None:
        """A failed DROP must not raise — the atexit hook must never crash."""
        from ovid2.drivers.snowflake.staging.gcs import SnowparkGcsStrategy

        atexit_hooks: list = []
        monkeypatch.setattr(
            "atexit.register", lambda fn: atexit_hooks.append(fn),
        )
        session = _FakeSession()

        # Make sql() raise for DROP STAGE.
        real_sql = session.sql

        def bombing_sql(q: str) -> Any:
            if q.startswith("DROP STAGE"):
                raise RuntimeError("boom")
            return real_sql(q)

        session.sql = bombing_sql  # type: ignore[method-assign]

        strategy = SnowparkGcsStrategy()
        strategy._maybe_schedule_cleanup(session, "inst-1")
        strategy._register_stage(session, "OVID2_STG_GCS_A")
        atexit_hooks[0]()  # must not raise
