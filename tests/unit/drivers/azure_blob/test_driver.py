"""Unit tests for :class:`ovid2.drivers.azure_blob.driver.AzureBlobDriver`.

Locks:

- ``AzureBlobDriver`` is registered in :data:`DRIVERS` under
  ``"azure_blob"`` as its native (not adapter) entry.
- ``as_legacy_src_args`` produces the pre-D3.azure_blob merged shape.
- ``auth_mode`` / ``probe_auth_mode`` return the correct classifier
  labels for every credential shape.
- ``read`` / ``write`` delegate to a cached :class:`AzureBlobIO`.
- ``probe_auth`` routes through
  :func:`probe_auth_from_config` (verified indirectly by asserting
  the ``code='skipped'`` path when the Azure SDK is absent).
- ``container_requirements`` currently returns an empty tuple (the
  ABFS driver (hadoop-azure) is baked into the runtime image).
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from ovid2.drivers.azure_blob import AzureBlobDriver
from ovid2.drivers.registry import DRIVERS


class TestNativeRegistration:
    """The registry entry points at the native driver, not an adapter."""

    def test_dotted_path_is_native(self) -> None:
        assert DRIVERS["azure_blob"] == ("ovid2.drivers.azure_blob.driver.AzureBlobDriver")

    def test_type_name_matches_registry_key(self) -> None:
        assert AzureBlobDriver.type_name == "azure_blob"


class TestAuthModeClassifier:
    """The driver-side auth-mode label — reads the address classifier."""

    def _make(self, **secrets: Any) -> AzureBlobDriver:
        return AzureBlobDriver(args={}, secrets=secrets, location={})

    def test_connection_string_wins(self) -> None:
        assert self._make(connection_string="x").auth_mode() == "connection_string"

    def test_account_key(self) -> None:
        assert self._make(account_key="k").auth_mode() == "account_key"

    def test_sas_token(self) -> None:
        assert self._make(sas_token="t").auth_mode() == "sas_token"

    def test_service_principal(self) -> None:
        d = self._make(tenant_id="T", client_id="C", client_secret="S")
        assert d.auth_mode() == "service_principal"

    def test_managed_identity_default(self) -> None:
        assert self._make().auth_mode() == "managed_identity"


class TestProbeAuthModeClassifier:
    """The connection-side auth-mode label — fifth leg is ``default_chain``."""

    def test_default_chain_when_all_absent(self) -> None:
        driver = AzureBlobDriver(args={}, secrets={}, location={})
        assert driver.probe_auth_mode() == "default_chain"

    def test_service_principal(self) -> None:
        driver = AzureBlobDriver(
            args={},
            secrets={"tenant_id": "T", "client_id": "C", "client_secret": "S"},
            location={},
        )
        assert driver.probe_auth_mode() == "service_principal"


class TestLegacySrcArgs:
    """The pre-D3.azure_blob merged shape is preserved verbatim."""

    def test_merges_args_secrets_location(self) -> None:
        driver = AzureBlobDriver(
            args={"account_name": "acct", "endpoint_suffix": "core.windows.net"},
            secrets={"tenant_id": "T", "client_id": "C", "client_secret": "S"},
            location={"container": "landing", "path": "out/acme", "format": "parquet"},
        )
        src_name, merged = driver.as_legacy_src_args()
        assert src_name == "azure_blob"
        assert merged == {
            "account_name": "acct",
            "endpoint_suffix": "core.windows.net",
            "tenant_id": "T",
            "client_id": "C",
            "client_secret": "S",
            "container": "landing",
            "prefix": "out/acme",
            "format": "parquet",
        }

    def test_container_default_fallback(self) -> None:
        driver = AzureBlobDriver(
            args={"account_name": "acct", "container_default": "def-c"},
            secrets={},
            location={},
        )
        _, merged = driver.as_legacy_src_args()
        assert merged["container"] == "def-c"

    def test_container_default_not_in_output(self) -> None:
        """``container_default`` is a *connection*-level arg; it must not
        leak into the merged output as a top-level key."""
        driver = AzureBlobDriver(
            args={"container_default": "def-c"},
            secrets={},
            location={"container": "override"},
        )
        _, merged = driver.as_legacy_src_args()
        assert "container_default" not in merged
        assert merged["container"] == "override"


class TestProbeAuth:
    """``probe_auth`` goes through :func:`probe_auth_from_config`."""

    def test_returns_skipped_when_sdk_absent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for mod in ("azure", "azure.storage", "azure.storage.blob"):
            monkeypatch.setitem(sys.modules, mod, None)
        driver = AzureBlobDriver(
            args={"account_name": "acct"},
            secrets={},
            location={},
        )
        result = driver.probe_auth()
        assert result.ok is False
        assert result.code == "skipped"
        assert result.capability == "auth"


class TestBuildIo:
    """The :class:`AzureBlobIO` cache lives on the driver instance."""

    def test_cached_across_calls(self) -> None:
        driver = AzureBlobDriver(
            args={"account_name": "acct"},
            secrets={},
            location={"container": "c"},
        )
        io_a = driver._build_io()
        io_b = driver._build_io()
        assert io_a is io_b

    def test_refuses_missing_container(self) -> None:
        driver = AzureBlobDriver(
            args={"account_name": "acct"},
            secrets={},
            location={},
        )
        with pytest.raises(ValueError, match="'container'"):
            driver._build_io()

    def test_refuses_missing_account_name(self) -> None:
        driver = AzureBlobDriver(
            args={},
            secrets={},
            location={"container": "c"},
        )
        with pytest.raises(ValueError, match="'account_name'"):
            driver._build_io()


class TestReadWriteDispatch:
    """``read`` / ``write`` route through the cached ``AzureBlobIO``."""

    def test_read_calls_io_read(self) -> None:
        driver = AzureBlobDriver(
            args={"account_name": "acct"},
            secrets={},
            location={"container": "c"},
        )
        fake_io = MagicMock(name="AzureBlobIO")
        driver._io_cache = fake_io
        session = MagicMock(name="EngineSession")
        driver.read(
            session,
            "demo",
            "20260101",
            "demographic.csv",
            delimiter="|",
        )
        fake_io.read.assert_called_once_with(
            session,
            "demo",
            "20260101",
            "demographic.csv",
            "|",
        )

    def test_read_ignores_table_name_kwarg(self) -> None:
        driver = AzureBlobDriver(
            args={"account_name": "acct"},
            secrets={},
            location={"container": "c"},
        )
        fake_io = MagicMock(name="AzureBlobIO")
        driver._io_cache = fake_io
        session = MagicMock(name="EngineSession")
        driver.read(
            session,
            "demo",
            "20260101",
            "demographic.csv",
            table_name="ignored",
        )
        # table_name arrives as a kwarg but is dropped before delegation.
        _, kwargs = fake_io.read.call_args
        assert "table_name" not in kwargs

    def test_write_calls_io_write(self) -> None:
        driver = AzureBlobDriver(
            args={"account_name": "acct"},
            secrets={},
            location={"container": "c"},
        )
        fake_io = MagicMock(name="AzureBlobIO")
        driver._io_cache = fake_io
        session = MagicMock(name="EngineSession")
        df = MagicMock(name="DataFrame")
        driver.write(df, session, "demo", "20260101", "demographic")
        fake_io.write.assert_called_once_with(
            df,
            session,
            "demo",
            "20260101",
            "demographic",
            None,
        )


class TestContainerRequirements:
    """The ABFS driver is baked into the runtime image today (no JavaPackage)."""

    def test_empty_container_requirements(self) -> None:
        driver = AzureBlobDriver(
            args={"account_name": "acct"},
            secrets={},
            location={"container": "c"},
        )
        assert tuple(driver.container_requirements()) == ()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run with pytest.")
