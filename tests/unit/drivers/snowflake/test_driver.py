"""Unit tests for :class:`ovid2.drivers.snowflake.driver.SnowflakeDriver`.

Locks:

- ``SnowflakeDriver`` is registered in :data:`DRIVERS` under
  ``"snowflake"`` as its native (not adapter) entry.
- ``as_legacy_src_args`` produces the pre-D3.snowflake merged shape.
- ``probe_auth`` routes through :func:`probe_auth_from_config`
  (verified indirectly via the skip path when the SDK is absent).
- ``read`` / ``write`` delegate to a cached :class:`SnowflakeAddress`.
- ``declared_cdm`` returns ``location.get("cdm")`` (D6.0 hook).
- ``container_requirements`` currently returns an empty tuple (the
  Spark-Snowflake connector jars are baked into the base image).
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from ovid2.drivers.registry import DRIVERS
from ovid2.drivers.snowflake import SnowflakeDriver


class TestNativeRegistration:
    """The registry entry points at the native driver, not an adapter."""

    def test_dotted_path_is_native(self) -> None:
        assert DRIVERS["snowflake"] == (
            "ovid2.drivers.snowflake.driver.SnowflakeDriver"
        )

    def test_type_name_matches_registry_key(self) -> None:
        assert SnowflakeDriver.type_name == "snowflake"


class TestDeclaredCdm:
    """The D6.0 advisory type-stamping hook."""

    def test_returns_cdm_from_location(self) -> None:
        assert (
            SnowflakeDriver.declared_cdm({"cdm": "pcornet_7_0"}) == "pcornet_7_0"
        )

    def test_returns_none_when_absent(self) -> None:
        assert SnowflakeDriver.declared_cdm({}) is None

    def test_returns_none_when_blank(self) -> None:
        assert SnowflakeDriver.declared_cdm({"cdm": ""}) is None

    def test_returns_str_for_non_str_input(self) -> None:
        """The hook coerces truthy values to ``str`` for uniformity."""
        assert SnowflakeDriver.declared_cdm({"cdm": 123}) == "123"


class TestLegacySrcArgs:
    """The pre-D3.snowflake merged shape is preserved verbatim."""

    def test_password_auth(self, tmp_path: Any) -> None:
        driver = SnowflakeDriver(
            args={
                "account": "acct",
                "user": "u",
                "warehouse": "WH",
                "role": "R",
            },
            secrets={"password": "p"},
            location={"database": "DB", "schema": "SC"},
        )
        src_name, merged = driver.as_legacy_src_args()
        assert src_name == "snowflake"
        assert merged == {
            "account": "acct",
            "user": "u",
            "warehouse": "WH",
            "role": "R",
            "password": "p",
            "database": "DB",
            "schema": "SC",
        }

    def test_password_bypasses_pem_load_when_credentials_dir_missing(
        self, tmp_path: Any,
    ) -> None:
        driver = SnowflakeDriver(
            args={
                "account": "acct",
                "user": "u",
                "credentials_dir": str(tmp_path / "nowhere"),
            },
            secrets={"password": "p"},
            location={"database": "DB"},
        )
        _, merged = driver.as_legacy_src_args()
        assert merged["password"] == "p"
        assert "private_key" not in merged


class TestProbeAuth:
    """``probe_auth`` goes through :func:`probe_auth_from_config`."""

    def test_returns_skipped_when_driver_absent(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setitem(sys.modules, "snowflake", None)
        monkeypatch.setitem(sys.modules, "snowflake.connector", None)
        driver = SnowflakeDriver(
            args={"account": "a", "user": "u"}, secrets={}, location={},
        )
        result = driver.probe_auth()
        assert result.ok is False
        assert result.code == "skipped"
        assert result.capability == "auth"


class TestBuildAddress:
    """The :class:`SnowflakeAddress` cache lives on the driver instance."""

    def test_cached_across_calls(self) -> None:
        driver = SnowflakeDriver(
            args={"account": "a", "user": "u"},
            secrets={"password": "p"},
            location={"database": "DB"},
        )
        addr_a = driver._build_address()
        addr_b = driver._build_address()
        assert addr_a is addr_b

    def test_address_carries_cdm_stamp(self) -> None:
        driver = SnowflakeDriver(
            args={"account": "a", "user": "u"},
            secrets={"password": "p"},
            location={"database": "DB", "cdm": "pcornet_7_0"},
        )
        addr = driver._build_address()
        assert addr._cdm_name == "pcornet_7_0"

    def test_address_default_cdm_when_unstamped(self) -> None:
        driver = SnowflakeDriver(
            args={"account": "a", "user": "u"},
            secrets={"password": "p"},
            location={"database": "DB"},
        )
        addr = driver._build_address()
        # Falls back to the class-level CDM_NAMESPACE.
        assert addr._cdm_name == "pcornet_7_0"


class TestReadWriteDispatch:
    """``read`` / ``write`` route through the cached ``SnowflakeAddress``."""

    def test_read_calls_addr_read(self) -> None:
        driver = SnowflakeDriver(
            args={"account": "a", "user": "u"},
            secrets={"password": "p"},
            location={"database": "DB"},
        )
        fake_addr = MagicMock(name="SnowflakeAddress")
        driver._address_cache = fake_addr
        session = MagicMock(name="EngineSession")
        driver.read(
            session, "demo", "20260101", "demographic",
            delimiter=None, table_name="demographic",
        )
        fake_addr.read.assert_called_once_with(
            session, "demo", "20260101", "demographic", None,
            table_name="demographic",
        )

    def test_write_calls_addr_write(self) -> None:
        driver = SnowflakeDriver(
            args={"account": "a", "user": "u"},
            secrets={"password": "p"},
            location={"database": "DB"},
        )
        fake_addr = MagicMock(name="SnowflakeAddress")
        driver._address_cache = fake_addr
        session = MagicMock(name="EngineSession")
        df = MagicMock(name="DataFrame")
        driver.write(df, session, "demo", "20260101", "demographic")
        fake_addr.write.assert_called_once_with(
            df, session, "demo", "20260101", "demographic", None,
        )


class TestContainerRequirements:
    """The Spark-Snowflake connector jars ship with the Bitnami image today."""

    def test_empty_container_requirements(self) -> None:
        driver = SnowflakeDriver(
            args={"account": "a", "user": "u"},
            secrets={"password": "p"},
            location={"database": "DB"},
        )
        assert tuple(driver.container_requirements()) == ()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run with pytest.")
