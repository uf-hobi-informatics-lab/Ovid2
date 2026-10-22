"""Unit tests for :mod:`ovid2.drivers.collector`."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ovid2.drivers.collector import (
    FORBIDDEN_CONTAINER_PATHS,
    RESERVED_CONTAINER_PATHS,
    collect_for_addresses,
)
from ovid2.drivers.container import (
    EnvOverlay,
    ExtraMount,
    JavaPackage,
    MergedRequirements,
)


@pytest.fixture
def config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated XDG_CONFIG_HOME with a configs/connections tree."""
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


def _write_conn(xdg: Path, name: str, payload: dict) -> None:
    (xdg / "ovid2" / "connections" / f"{name}.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


def _write_addr(xdg: Path, name: str, payload: dict) -> None:
    (xdg / "ovid2" / "configs" / f"{name}.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


class TestCollectForAddresses:
    def test_empty_input_returns_empty(self, config_env: Path) -> None:
        got = collect_for_addresses([])
        assert got == MergedRequirements()

    def test_skips_missing_addresses(self, config_env: Path) -> None:
        got = collect_for_addresses(["nope"])
        assert got == MergedRequirements()

    def test_w7_disc_produces_extra_mount(
        self, config_env: Path, tmp_path: Path,
    ) -> None:
        host = tmp_path / "d"
        host.mkdir()
        _write_conn(config_env, "d_conn", {
            "type": "disc",
            "args": {"host_path": str(host), "mount_mode": "rw"},
        })
        _write_addr(config_env, "d_addr", {
            "connection": "d_conn", "location": {"path": "sub"},
        })
        got = collect_for_addresses(["d_addr"])
        assert len(got.mounts) == 1
        assert got.mounts[0].host_path == str(host)
        assert got.mounts[0].container_path == str(host)
        assert got.mounts[0].mode == "rw"

    def test_legacy_disc_produces_extra_mount(
        self, config_env: Path, tmp_path: Path,
    ) -> None:
        host = tmp_path / "legacy"
        host.mkdir()
        _write_addr(config_env, "d_legacy", {
            "src_name": "disc",
            "src_args": {"path": str(host)},
        })
        got = collect_for_addresses(["d_legacy"])
        assert len(got.mounts) == 1
        assert got.mounts[0].host_path == str(host)

    def test_dedup_same_container_path(
        self, config_env: Path, tmp_path: Path,
    ) -> None:
        """Two addresses landing on the same mount target dedup to one."""
        host = tmp_path / "shared"
        host.mkdir()
        _write_conn(config_env, "d_conn", {
            "type": "disc",
            "args": {"host_path": str(host)},
        })
        _write_addr(config_env, "a1", {"connection": "d_conn", "location": {}})
        _write_addr(config_env, "a2", {"connection": "d_conn", "location": {}})
        got = collect_for_addresses(["a1", "a2"])
        assert len(got.mounts) == 1

    def test_explicit_mount_overrides_auto(
        self, config_env: Path, tmp_path: Path,
    ) -> None:
        host = tmp_path / "d"
        host.mkdir()
        _write_conn(config_env, "d_conn", {
            "type": "disc",
            "args": {"host_path": str(host)},
        })
        _write_addr(config_env, "d_addr", {
            "connection": "d_conn", "location": {},
        })
        got = collect_for_addresses(
            ["d_addr"],
            explicit_container_paths=[str(host)],
        )
        assert got.mounts == ()

    def test_reserved_container_paths_dropped(
        self, config_env: Path,
    ) -> None:
        """A driver-declared mount on ``/data`` is silently dropped."""
        _write_conn(config_env, "d_conn", {
            "type": "disc",
            "args": {
                "host_path": "/data",
                "container_path": "/data",
                "mount_mode": "rw",
            },
        })
        _write_addr(config_env, "d_addr", {
            "connection": "d_conn", "location": {},
        })
        got = collect_for_addresses(["d_addr"])
        assert got.mounts == ()

    def test_forbidden_container_paths_dropped(
        self, config_env: Path,
    ) -> None:
        _write_conn(config_env, "d_conn", {
            "type": "disc",
            "args": {
                "host_path": "/",
                "container_path": "/",
                "mount_mode": "rw",
            },
        })
        _write_addr(config_env, "d_addr", {
            "connection": "d_conn", "location": {},
        })
        got = collect_for_addresses(["d_addr"])
        assert got.mounts == ()

    def test_non_disc_type_produces_no_mounts_today(
        self, config_env: Path,
    ) -> None:
        """Under D2 only disc contributes mounts through the adapter.

        Native driver ports (D3.<type>) will move S3 Java-package
        requirements here. Until then, non-disc addresses contribute
        nothing.
        """
        _write_conn(config_env, "s_conn", {
            "type": "s3",
            "args": {"region": "us-east-1", "bucket_default": "b"},
        })
        _write_addr(config_env, "s_addr", {
            "connection": "s_conn", "location": {"path": "prefix"},
        })
        got = collect_for_addresses(["s_addr"])
        assert got.mounts == ()
        assert got.java_packages == ()
        assert got.env_overlays == ()

    def test_missing_connection_json_is_silent(
        self, config_env: Path,
    ) -> None:
        _write_addr(config_env, "orphan", {
            "connection": "nope", "location": {},
        })
        got = collect_for_addresses(["orphan"])
        assert got == MergedRequirements()

    def test_empty_address_name_is_skipped(self, config_env: Path) -> None:
        got = collect_for_addresses(["", None])  # type: ignore[list-item]
        assert got == MergedRequirements()

    def test_relative_container_paths_dropped(
        self, config_env: Path, tmp_path: Path,
    ) -> None:
        """Container paths must be absolute; relative ones are rejected."""
        host = tmp_path / "d"
        host.mkdir()
        _write_conn(config_env, "d_conn", {
            "type": "disc",
            "args": {
                "host_path": str(host),
                "container_path": "relative/path",
            },
        })
        _write_addr(config_env, "d_addr", {
            "connection": "d_conn", "location": {},
        })
        got = collect_for_addresses(["d_addr"])
        assert got.mounts == ()


class TestReservedAndForbiddenSets:
    def test_reserved_paths_include_standard_mounts(self) -> None:
        assert "/data" in RESERVED_CONTAINER_PATHS
        assert "/app" in RESERVED_CONTAINER_PATHS
        assert "/ovid_credentials" in RESERVED_CONTAINER_PATHS
        assert "/usr/include/rsa" in RESERVED_CONTAINER_PATHS

    def test_forbidden_paths_include_root_and_system_dirs(self) -> None:
        assert "/" in FORBIDDEN_CONTAINER_PATHS
        assert "/etc" in FORBIDDEN_CONTAINER_PATHS
        assert "/proc" in FORBIDDEN_CONTAINER_PATHS

    def test_reserved_and_forbidden_are_disjoint(self) -> None:
        assert RESERVED_CONTAINER_PATHS & FORBIDDEN_CONTAINER_PATHS == set()


class TestSecretRefsForAddresses:
    """Secret-ref discovery for executor least-privilege mounts."""

    def test_empty_input_returns_empty(self, config_env: Path) -> None:
        from ovid2.drivers.collector import secret_refs_for_addresses

        assert secret_refs_for_addresses([]) == set()

    def test_missing_addresses_skipped(self, config_env: Path) -> None:
        from ovid2.drivers.collector import secret_refs_for_addresses

        assert secret_refs_for_addresses(["nope"]) == set()

    def test_w7_address_returns_connection_secret_ref(
        self, config_env: Path
    ) -> None:
        from ovid2.drivers.collector import secret_refs_for_addresses

        _write_conn(config_env, "sf_conn", {
            "type": "snowflake",
            "args": {"account": "acct", "user": "u"},
            "secret_ref": "snowflake_prod",
        })
        _write_addr(config_env, "sf_addr", {
            "connection": "sf_conn",
            "location": {"database": "DB", "schema": "S"},
        })
        assert secret_refs_for_addresses(["sf_addr"]) == {"snowflake_prod"}

    def test_multiple_addresses_union_refs(self, config_env: Path) -> None:
        from ovid2.drivers.collector import secret_refs_for_addresses

        _write_conn(config_env, "a_conn", {
            "type": "snowflake", "args": {"account": "a", "user": "u"},
            "secret_ref": "ref_a",
        })
        _write_conn(config_env, "b_conn", {
            "type": "snowflake", "args": {"account": "b", "user": "u"},
            "secret_ref": "ref_b",
        })
        _write_addr(config_env, "a_addr", {"connection": "a_conn", "location": {}})
        _write_addr(config_env, "b_addr", {"connection": "b_conn", "location": {}})
        got = secret_refs_for_addresses(["a_addr", "b_addr"])
        assert got == {"ref_a", "ref_b"}

    def test_connection_without_secret_ref_contributes_nothing(
        self, config_env: Path
    ) -> None:
        from ovid2.drivers.collector import secret_refs_for_addresses

        _write_conn(config_env, "no_secret", {
            "type": "snowflake", "args": {"account": "a", "user": "u"},
        })
        _write_addr(config_env, "ns_addr", {"connection": "no_secret", "location": {}})
        assert secret_refs_for_addresses(["ns_addr"]) == set()
