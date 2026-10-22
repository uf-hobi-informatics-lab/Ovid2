"""Unit tests for :mod:`ovid2.drivers.type_stamping`."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from ovid2.drivers.type_stamping import (
    TypeMismatch,
    declared_cdm_for_address,
    validate_route_types,
)


@pytest.fixture
def config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv(
        "OVID2_CONFIGS_DIR", str(xdg / "ovid2" / "configs"),
    )
    monkeypatch.setenv(
        "OVID2_CONNECTIONS_DIR", str(xdg / "ovid2" / "connections"),
    )
    (xdg / "ovid2" / "configs").mkdir(parents=True)
    (xdg / "ovid2" / "connections").mkdir(parents=True)
    return xdg


def _conn(xdg: Path, name: str, payload: dict) -> None:
    (xdg / "ovid2" / "connections" / f"{name}.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


def _addr(xdg: Path, name: str, payload: dict) -> None:
    (xdg / "ovid2" / "configs" / f"{name}.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


class TestDeclaredCdmForAddress:
    def test_missing_address_returns_none(self, config_env: Path) -> None:
        assert declared_cdm_for_address("nope") is None

    def test_disc_never_declares_cdm(
        self, config_env: Path, tmp_path: Path,
    ) -> None:
        host = tmp_path / "d"
        host.mkdir()
        _conn(config_env, "d_conn", {
            "type": "disc",
            "args": {"host_path": str(host)},
        })
        _addr(config_env, "a", {"connection": "d_conn", "location": {}})
        assert declared_cdm_for_address("a") is None

    def test_w7_snowflake_with_cdm_declares(
        self, config_env: Path,
    ) -> None:
        _conn(config_env, "sf", {
            "type": "snowflake",
            "args": {"account": "acct", "user": "u"},
        })
        _addr(config_env, "sf_addr", {
            "connection": "sf",
            "location": {"database": "DB", "cdm": "pcornet_7_0"},
        })
        assert declared_cdm_for_address("sf_addr") == "pcornet_7_0"

    def test_w7_snowflake_without_cdm_returns_none(
        self, config_env: Path,
    ) -> None:
        _conn(config_env, "sf", {
            "type": "snowflake",
            "args": {"account": "acct", "user": "u"},
        })
        _addr(config_env, "sf_addr", {
            "connection": "sf",
            "location": {"database": "DB"},
        })
        assert declared_cdm_for_address("sf_addr") is None

    def test_legacy_snowflake_with_cdm_declares(
        self, config_env: Path,
    ) -> None:
        _addr(config_env, "sf_legacy", {
            "src_name": "snowflake",
            "src_args": {
                "account": "acct", "user": "u",
                "database": "DB", "cdm": "pcornet_7_0",
            },
        })
        assert declared_cdm_for_address("sf_legacy") == "pcornet_7_0"


class TestValidateRouteTypes:
    def test_agreeing_cdms_pass_silently(
        self, config_env: Path,
    ) -> None:
        _conn(config_env, "sf", {
            "type": "snowflake",
            "args": {"account": "acct", "user": "u"},
        })
        _addr(config_env, "src", {
            "connection": "sf",
            "location": {"database": "S", "cdm": "pcornet_7_0"},
        })
        _addr(config_env, "dst", {
            "connection": "sf",
            "location": {"database": "T", "cdm": "pcornet_7_0"},
        })
        got = validate_route_types("r1", "src", "dst")
        assert got is None

    def test_mismatched_cdms_return_finding(
        self, config_env: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        _conn(config_env, "sf", {
            "type": "snowflake",
            "args": {"account": "acct", "user": "u"},
        })
        _addr(config_env, "src", {
            "connection": "sf",
            "location": {"database": "S", "cdm": "pcornet_7_0"},
        })
        _addr(config_env, "dst", {
            "connection": "sf",
            "location": {"database": "T", "cdm": "omop_5_4"},
        })
        with caplog.at_level(logging.WARNING, logger="ovid2.drivers.type_stamping"):
            got = validate_route_types("r_mismatch", "src", "dst")
        assert isinstance(got, TypeMismatch)
        assert got.from_cdm == "pcornet_7_0"
        assert got.to_cdm == "omop_5_4"
        assert got.route_name == "r_mismatch"
        assert "Type mismatch" in caplog.text
        assert "pcornet_7_0" in caplog.text
        assert "omop_5_4" in caplog.text

    def test_strict_raises_valueerror(
        self, config_env: Path,
    ) -> None:
        _conn(config_env, "sf", {
            "type": "snowflake",
            "args": {"account": "acct", "user": "u"},
        })
        _addr(config_env, "src", {
            "connection": "sf",
            "location": {"database": "S", "cdm": "pcornet_7_0"},
        })
        _addr(config_env, "dst", {
            "connection": "sf",
            "location": {"database": "T", "cdm": "other"},
        })
        with pytest.raises(ValueError, match="Type mismatch|pcornet_7_0"):
            validate_route_types("r_strict", "src", "dst", strict=True)

    def test_one_side_unknown_is_compatible(
        self, config_env: Path,
    ) -> None:
        """Legacy corpus: if either side doesn't declare, treat as compatible."""
        _conn(config_env, "sf", {
            "type": "snowflake",
            "args": {"account": "acct", "user": "u"},
        })
        _addr(config_env, "src", {
            "connection": "sf",
            "location": {"database": "S", "cdm": "pcornet_7_0"},
        })
        _addr(config_env, "dst", {
            "connection": "sf",
            "location": {"database": "T"},
        })
        got = validate_route_types("r_partial", "src", "dst")
        assert got is None

    def test_disc_source_never_flags(
        self, config_env: Path, tmp_path: Path,
    ) -> None:
        """Disc addresses never declare CDMs → no advisory ever fires."""
        host = tmp_path / "d"
        host.mkdir()
        _conn(config_env, "d", {"type": "disc", "args": {"host_path": str(host)}})
        _conn(config_env, "sf", {
            "type": "snowflake", "args": {"account": "a", "user": "u"},
        })
        _addr(config_env, "src", {"connection": "d", "location": {}})
        _addr(config_env, "dst", {
            "connection": "sf",
            "location": {"database": "T", "cdm": "pcornet_7_0"},
        })
        assert validate_route_types("r", "src", "dst") is None


class TestTypeMismatch:
    def test_message_shape(self) -> None:
        m = TypeMismatch(
            route_name="r", from_address="a", to_address="b",
            from_cdm="x", to_cdm="y",
        )
        msg = m.message()
        assert "r" in msg and "a" in msg and "b" in msg
        assert "'x'" in msg and "'y'" in msg
