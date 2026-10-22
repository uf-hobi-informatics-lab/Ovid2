"""Unit tests for :class:`ovid2.drivers.snowflake.staging.azure_blob.SnowparkAzureBlobStrategy`.

Every test drives the strategy through fake Snowpark session doubles
so the suite runs without live Snowflake / Azure credentials.
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
    monkeypatch.setenv("OVID2_INSTANCE_ID", "20260806-150000-abcdef")
    monkeypatch.delenv("OVID2_SNOWPARK_STAGING_KEEP", raising=False)
    monkeypatch.delenv(
        "OVID2_SNOWPARK_STAGING_AZURE_BLOB_INTEGRATION", raising=False,
    )
    monkeypatch.delenv("OVID2_SNOWPARK_STAGING_SCHEMA", raising=False)


@pytest.fixture(autouse=True)
def _clear_strategy_state():
    from ovid2.drivers.snowflake.staging.azure_blob import (
        SnowparkAzureBlobStrategy,
    )

    SnowparkAzureBlobStrategy._cleanups_scheduled.clear()
    SnowparkAzureBlobStrategy._stages.clear()
    SnowparkAzureBlobStrategy._temp_tables.clear()
    yield
    SnowparkAzureBlobStrategy._cleanups_scheduled.clear()
    SnowparkAzureBlobStrategy._stages.clear()
    SnowparkAzureBlobStrategy._temp_tables.clear()


class _FakeSession:
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


class _FakeAzureDriver:
    """Stand-in for :class:`AzureBlobDriver`."""

    type_name = "azure_blob"

    def __init__(self, merged: dict[str, Any]) -> None:
        self._merged = merged

    def as_legacy_src_args(self) -> tuple[str, dict[str, Any]]:
        return "azure_blob", dict(self._merged)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_strategy_registered_under_azure_blob(self) -> None:
        import ovid2.drivers.snowflake  # noqa: F401

        assert "azure_blob" in SnowparkEngineStager._STRATEGIES
        assert SnowparkEngineStager.supports("azure_blob") is True

    def test_registered_class_is_a_snowpark_strategy(self) -> None:
        import ovid2.drivers.snowflake  # noqa: F401

        cls = SnowparkEngineStager._STRATEGIES["azure_blob"]
        assert issubclass(cls, SnowparkStagingStrategy)
        assert cls.driver_type == "azure_blob"


# ---------------------------------------------------------------------------
# Import-safety
# ---------------------------------------------------------------------------


class TestImportSafety:
    def test_module_scope_does_not_import_azure_sdk(self) -> None:
        for mod in list(sys.modules):
            if mod == "ovid2.drivers.snowflake.staging.azure_blob":
                del sys.modules[mod]
            if mod.startswith("azure.storage") or mod.startswith("azure.identity"):
                del sys.modules[mod]
        import ovid2.drivers.snowflake.staging.azure_blob  # noqa: F401

        # Neither SDK is present after the module import.
        for m in sys.modules:
            assert not m.startswith("azure.storage"), f"azure.storage leaked: {m}"
            assert not m.startswith("azure.identity"), f"azure.identity leaked: {m}"

    def test_module_scope_does_not_import_pyspark(self) -> None:
        for mod in list(sys.modules):
            if mod == "ovid2.drivers.snowflake.staging.azure_blob":
                del sys.modules[mod]
        pyspark_before = {m for m in sys.modules if m.startswith("pyspark")}
        import ovid2.drivers.snowflake.staging.azure_blob  # noqa: F401
        pyspark_after = {m for m in sys.modules if m.startswith("pyspark")}
        assert pyspark_after == pyspark_before


# ---------------------------------------------------------------------------
# _build_create_stage_sql — auth path resolution
# ---------------------------------------------------------------------------


class TestBuildCreateStageSql:
    def test_prefers_storage_integration_when_set(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging import azure_blob

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_AZURE_BLOB_INTEGRATION", "AZ_INT",
        )
        sql = azure_blob._build_create_stage_sql(
            stage_name="OVID2_STG_AZ_X",
            url="azure://acct.blob.core.windows.net/c/prefix/",
            merged_config={"sas_token": "sig=abc"},
            container="c",
        )
        assert "STORAGE_INTEGRATION = AZ_INT" in sql
        assert "AZURE_SAS_TOKEN" not in sql

    def test_falls_back_to_sas_token(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging import azure_blob

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_AZURE_BLOB_INTEGRATION", None,
        )
        sql = azure_blob._build_create_stage_sql(
            stage_name="OVID2_STG_AZ_X",
            url="azure://acct.blob.core.windows.net/c/",
            merged_config={"sas_token": "?sig=abc&sr=c"},
            container="c",
        )
        # Leading '?' is stripped.
        assert "AZURE_SAS_TOKEN = 'sig=abc&sr=c'" in sql

    def test_falls_back_to_generated_sas_from_account_key(
        self, monkeypatch,
    ) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging import azure_blob

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_AZURE_BLOB_INTEGRATION", None,
        )
        # Stub the SAS generator so the test doesn't need the real
        # azure-storage-blob package.
        monkeypatch.setattr(
            azure_blob, "_generate_container_sas",
            lambda **kw: "sig=generated&se=2026",
        )
        sql = azure_blob._build_create_stage_sql(
            stage_name="X",
            url="azure://acct.blob.core.windows.net/c/",
            merged_config={
                "account_name": "acct",
                "account_key": "kkkkk",
            },
            container="c",
        )
        assert "AZURE_SAS_TOKEN = 'sig=generated&se=2026'" in sql

    def test_extracts_account_key_from_connection_string(
        self, monkeypatch,
    ) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging import azure_blob

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_AZURE_BLOB_INTEGRATION", None,
        )
        captured = {}

        def _fake_generate(**kw: Any) -> str:
            captured.update(kw)
            return "sig=fromconn"

        monkeypatch.setattr(
            azure_blob, "_generate_container_sas", _fake_generate,
        )
        sql = azure_blob._build_create_stage_sql(
            stage_name="X",
            url="azure://acct.blob.core.windows.net/c/",
            merged_config={
                "account_name": "acct",
                "connection_string": (
                    "DefaultEndpointsProtocol=https;"
                    "AccountName=acct;"
                    "AccountKey=extractedkey;"
                    "EndpointSuffix=core.windows.net"
                ),
            },
            container="c",
        )
        assert "AZURE_SAS_TOKEN = 'sig=fromconn'" in sql
        assert captured["account_key"] == "extractedkey"

    def test_raises_when_no_auth_available(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging import azure_blob

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_AZURE_BLOB_INTEGRATION", None,
        )
        with pytest.raises(ValueError, match="no Snowflake→Azure auth path"):
            azure_blob._build_create_stage_sql(
                stage_name="X",
                url="azure://a.blob.core.windows.net/c/",
                merged_config={"account_name": "a"},
                container="c",
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_azure_dir_url_composition(self) -> None:
        from ovid2.drivers.snowflake.staging.azure_blob import _azure_dir_url

        assert _azure_dir_url(
            "acct", "core.windows.net", "container", "prefix", "demo", "20260101",
        ) == "azure://acct.blob.core.windows.net/container/prefix/demo/20260101/"
        assert _azure_dir_url(
            "acct", "core.windows.net", "container", "", "", "",
        ) == "azure://acct.blob.core.windows.net/container/"

    def test_azure_blob_name_composition(self) -> None:
        from ovid2.drivers.snowflake.staging.azure_blob import _azure_blob_name

        assert _azure_blob_name("prefix", "demo", "20260101", "f.csv") == (
            "prefix/demo/20260101/f.csv"
        )
        assert _azure_blob_name("", "demo", "20260101", "f.csv") == (
            "demo/20260101/f.csv"
        )

    def test_stage_name_uppercased_and_sanitised(self) -> None:
        from ovid2.drivers.snowflake.staging.azure_blob import _stage_name

        name = _stage_name(
            "20260806-150000-abc", "demo", "20260101", "encounter", kind="in",
        )
        assert name.startswith("OVID2_STG_AZ_")
        assert name == name.upper()

    def test_require_account_from_connection_string(self) -> None:
        from ovid2.drivers.snowflake.staging.azure_blob import _require_account

        got = _require_account({
            "connection_string": (
                "DefaultEndpointsProtocol=https;AccountName=fromconn;"
                "AccountKey=k;EndpointSuffix=core.windows.net"
            ),
        })
        assert got == "fromconn"

    def test_require_account_prefers_explicit(self) -> None:
        from ovid2.drivers.snowflake.staging.azure_blob import _require_account

        got = _require_account({
            "account_name": "explicit",
            "connection_string": (
                "AccountName=fromconn;AccountKey=k;"
                "EndpointSuffix=core.windows.net"
            ),
        })
        assert got == "explicit"

    def test_require_account_raises_when_missing(self) -> None:
        from ovid2.drivers.snowflake.staging.azure_blob import _require_account

        with pytest.raises(ValueError, match="no storage account resolved"):
            _require_account({})

    def test_require_container_raises_when_missing(self) -> None:
        from ovid2.drivers.snowflake.staging.azure_blob import _require_container

        with pytest.raises(ValueError, match="no container resolved"):
            _require_container({"account_name": "a"})

    def test_endpoint_suffix_default(self) -> None:
        from ovid2.drivers.snowflake.staging.azure_blob import _endpoint_suffix

        assert _endpoint_suffix({}) == "core.windows.net"
        assert _endpoint_suffix({"endpoint_suffix": "core.usgovcloudapi.net"}) == (
            "core.usgovcloudapi.net"
        )


# ---------------------------------------------------------------------------
# materialise — happy path with a stubbed Azure Blob header reader
# ---------------------------------------------------------------------------


class TestMaterialise:
    def _prepare_azure_stub(self, monkeypatch, body: bytes) -> None:
        """Install a fake azure.storage.blob returning ``body`` from download."""
        fake_blob_client = MagicMock()
        fake_stream = MagicMock()
        fake_stream.readall.return_value = body
        fake_blob_client.download_blob.return_value = fake_stream
        fake_service_client = MagicMock()
        fake_service_client.get_blob_client.return_value = fake_blob_client
        fake_service_client_cls = MagicMock(return_value=fake_service_client)
        fake_service_client_cls.from_connection_string = MagicMock(
            return_value=fake_service_client,
        )

        # Build a hierarchical fake for azure.storage.blob.
        fake_blob_mod = MagicMock()
        fake_blob_mod.BlobServiceClient = fake_service_client_cls
        fake_blob_mod.ContainerSasPermissions = MagicMock
        fake_blob_mod.generate_container_sas = MagicMock(return_value="sig=fake")
        monkeypatch.setitem(sys.modules, "azure.storage.blob", fake_blob_mod)

        # Also stub azure.identity in case we go down the default-cred path.
        fake_identity_mod = MagicMock()
        fake_identity_mod.DefaultAzureCredential = MagicMock
        monkeypatch.setitem(sys.modules, "azure.identity", fake_identity_mod)

    def test_materialise_happy_path_with_account_key(
        self, monkeypatch,
    ) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging import azure_blob
        from ovid2.drivers.snowflake.staging.azure_blob import (
            SnowparkAzureBlobStrategy,
        )

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_AZURE_BLOB_INTEGRATION", None,
        )
        # Stub the SAS generator so the CREATE STAGE has a token.
        monkeypatch.setattr(
            azure_blob, "_generate_container_sas",
            lambda **kw: "sig=fake",
        )
        self._prepare_azure_stub(
            monkeypatch, body=b"COL_A\tCOL_B\n1\t2\n",
        )

        session = _FakeSession()
        driver = _FakeAzureDriver({
            "account_name": "acct",
            "container": "c",
            "prefix": "raw",
            "endpoint_suffix": "core.windows.net",
            "account_key": "key123",
            "format": "csv",
        })
        strategy = SnowparkAzureBlobStrategy()

        df = strategy.materialise(
            session, driver, "demo", "20260101",
            "encounter.csv", None, None,
        )

        create_stage = [q for q in session.sql_calls if q.startswith("CREATE OR REPLACE TEMPORARY STAGE")]
        create_table = [q for q in session.sql_calls if q.startswith("CREATE OR REPLACE TRANSIENT TABLE")]
        copy_into = [q for q in session.sql_calls if q.startswith("COPY INTO")]
        assert len(create_stage) == 1
        assert len(create_table) == 1
        assert len(copy_into) == 1

        # Stage URL uses the azure:// scheme with .blob. subdomain.
        assert "azure://acct.blob.core.windows.net/c/raw/demo/20260101/" in create_stage[0]
        # SAS token was inlined.
        assert "AZURE_SAS_TOKEN = 'sig=fake'" in create_stage[0]
        # Columns from the header.
        assert '"COL_A" VARCHAR' in create_table[0]
        assert '"COL_B" VARCHAR' in create_table[0]
        assert "encounter.csv" in copy_into[0]
        assert df is not None

    def test_materialise_rejects_non_csv(self) -> None:
        from ovid2.drivers.snowflake.staging.azure_blob import (
            SnowparkAzureBlobStrategy,
        )

        session = _FakeSession()
        driver = _FakeAzureDriver({
            "account_name": "a", "container": "c", "format": "parquet",
        })
        strategy = SnowparkAzureBlobStrategy()
        with pytest.raises(ValueError, match="only supports fmt='csv'"):
            strategy.materialise(
                session, driver, "demo", "20260101", "f.parquet", None, None,
            )

    def test_materialise_requires_account(self) -> None:
        from ovid2.drivers.snowflake.staging.azure_blob import (
            SnowparkAzureBlobStrategy,
        )

        session = _FakeSession()
        driver = _FakeAzureDriver({"container": "c", "format": "csv"})
        strategy = SnowparkAzureBlobStrategy()
        with pytest.raises(ValueError, match="no storage account resolved"):
            strategy.materialise(
                session, driver, "p", "b", "f.csv", None, None,
            )


# ---------------------------------------------------------------------------
# persist
# ---------------------------------------------------------------------------


class TestPersist:
    def test_persist_creates_stage_and_calls_copy_into_location(
        self, monkeypatch,
    ) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.azure_blob import (
            SnowparkAzureBlobStrategy,
        )

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_AZURE_BLOB_INTEGRATION", "AZ_INT",
        )
        session = _FakeSession()
        driver = _FakeAzureDriver({
            "account_name": "acct",
            "container": "outc",
            "prefix": "cooked",
            "endpoint_suffix": "core.windows.net",
            "format": "csv",
            "single_file": True,
        })
        strategy = SnowparkAzureBlobStrategy()
        df = MagicMock()
        strategy.persist(
            session, df, driver, "demo", "20260101", "encounter", None,
        )

        create_stage = [q for q in session.sql_calls if q.startswith("CREATE OR REPLACE TEMPORARY STAGE")]
        assert len(create_stage) == 1
        assert "azure://acct.blob.core.windows.net/outc/cooked/demo/20260101/" in create_stage[0]
        assert "STORAGE_INTEGRATION = AZ_INT" in create_stage[0]

        # No GET step — Snowflake writes directly.
        assert not any(q.startswith("GET ") for q in session.sql_calls)
        df.write.copy_into_location.assert_called_once()
        args, kwargs = df.write.copy_into_location.call_args
        assert args[0].endswith("/encounter/")
        assert kwargs["single"] is True

    def test_persist_defaults_single_file_false(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.azure_blob import (
            SnowparkAzureBlobStrategy,
        )

        monkeypatch.setattr(
            settings, "SNOWPARK_STAGING_AZURE_BLOB_INTEGRATION", "I",
        )
        session = _FakeSession()
        driver = _FakeAzureDriver({
            "account_name": "a", "container": "c",
            "format": "csv",
        })
        df = MagicMock()
        strategy = SnowparkAzureBlobStrategy()
        strategy.persist(session, df, driver, "p", "b", "t", None)
        _, kwargs = df.write.copy_into_location.call_args
        assert kwargs["single"] is False


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


class TestCleanup:
    def test_cleanup_registered_once_per_instance(self, monkeypatch) -> None:
        from ovid2.drivers.snowflake.staging.azure_blob import (
            SnowparkAzureBlobStrategy,
        )

        atexit_calls: list = []
        monkeypatch.setattr(
            "atexit.register", lambda fn: atexit_calls.append(fn),
        )
        session = _FakeSession()
        strategy = SnowparkAzureBlobStrategy()
        strategy._maybe_schedule_cleanup(session, "inst-1")
        strategy._maybe_schedule_cleanup(session, "inst-1")
        strategy._maybe_schedule_cleanup(session, "inst-2")
        assert len(atexit_calls) == 2

    def test_cleanup_skipped_when_keep_set(self, monkeypatch) -> None:
        from ovid2.config import settings
        from ovid2.drivers.snowflake.staging.azure_blob import (
            SnowparkAzureBlobStrategy,
        )

        atexit_calls: list = []
        monkeypatch.setattr(
            "atexit.register", lambda fn: atexit_calls.append(fn),
        )
        monkeypatch.setattr(settings, "SNOWPARK_STAGING_KEEP", True)
        session = _FakeSession()
        strategy = SnowparkAzureBlobStrategy()
        strategy._maybe_schedule_cleanup(session, "inst-1")
        assert atexit_calls == []

    def test_cleanup_drops_stages_and_tables(self, monkeypatch) -> None:
        from ovid2.drivers.snowflake.staging.azure_blob import (
            SnowparkAzureBlobStrategy,
        )

        atexit_hooks: list = []
        monkeypatch.setattr(
            "atexit.register", lambda fn: atexit_hooks.append(fn),
        )
        session = _FakeSession()
        strategy = SnowparkAzureBlobStrategy()
        strategy._maybe_schedule_cleanup(session, "inst-1")
        strategy._register_stage(session, "OVID2_STG_AZ_A")
        strategy._register_temp_table(session, "DB.SCH.OVID2_STG_AZ_A")

        assert len(atexit_hooks) == 1
        atexit_hooks[0]()
        drop_stages = [q for q in session.sql_calls if q.startswith("DROP STAGE")]
        drop_tables = [q for q in session.sql_calls if q.startswith("DROP TABLE")]
        assert "DROP STAGE IF EXISTS OVID2_STG_AZ_A" in drop_stages
        assert "DROP TABLE IF EXISTS DB.SCH.OVID2_STG_AZ_A" in drop_tables
