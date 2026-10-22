"""Tests for the W7 ``ovid2 tools migrate-addresses`` CLI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from ovid2.tools import migrate_addresses


@pytest.fixture()
def clean_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """chdir into a scratch dir so the repo-local ``configs/`` never leaks in."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.unit
def test_dry_run_leaves_disk_untouched(isolated_xdg: Path, clean_cwd: Path) -> None:
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    configs.mkdir(parents=True, exist_ok=True)
    (configs / "legacy.json").write_text(
        json.dumps({"src_name": "disc", "src_args": {"path": "/data/x"}})
    )
    rc = migrate_addresses.main(argparse.Namespace(write=False, only=[]))
    assert rc == 0
    # Address JSON must not have been rewritten.
    on_disk = json.loads((configs / "legacy.json").read_text())
    assert on_disk == {"src_name": "disc", "src_args": {"path": "/data/x"}}


@pytest.mark.unit
def test_migrate_disc_produces_connection_and_empty_location(
    isolated_xdg: Path, clean_cwd: Path
) -> None:
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    configs.mkdir(parents=True, exist_ok=True)
    (configs / "legacy.json").write_text(
        json.dumps({"src_name": "disc", "src_args": {"path": "/data/xyz"}})
    )
    rc = migrate_addresses.main(argparse.Namespace(write=True, only=[]))
    assert rc == 0
    address = json.loads((configs / "legacy.json").read_text())
    assert "src_name" not in address
    assert address["connection"]
    assert address["location"] == {}
    connections_dir = Path(os.environ["OVID2_CONNECTIONS_DIR"])
    connection_files = list(connections_dir.iterdir())
    assert len(connection_files) == 1
    conn = json.loads(connection_files[0].read_text())
    assert conn["type"] == "disc"
    assert conn["args"]["host_path"] == "/data/xyz"


@pytest.mark.unit
def test_migrate_snowflake_writes_secret_with_0600(isolated_xdg: Path, clean_cwd: Path) -> None:
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    configs.mkdir(parents=True, exist_ok=True)
    (configs / "sf.json").write_text(
        json.dumps(
            {
                "src_name": "snowflake",
                "src_args": {
                    "account": "a",
                    "user": "u",
                    "password": "p",
                    "database": "D",
                    "schema": "S",
                },
            }
        )
    )
    rc = migrate_addresses.main(argparse.Namespace(write=True, only=[]))
    assert rc == 0
    # Address ends up in new shape.
    address = json.loads((configs / "sf.json").read_text())
    assert address["connection"] == "sf_a_u"
    assert address["location"] == {"database": "D", "schema": "S"}
    # Connection has the account/user; no password inline.
    conn_dir = Path(os.environ["OVID2_CONNECTIONS_DIR"])
    conn = json.loads((conn_dir / "sf_a_u.json").read_text())
    assert conn["args"] == {"account": "a", "user": "u"}
    assert conn["secret_ref"] == "conn_sf_a_u"
    assert "password" not in json.dumps(conn)
    # Secret file is mode 0o600 and carries the password.
    secret_file = isolated_xdg / "config" / "ovid2" / "secrets.d" / "conn_sf_a_u.json"
    assert secret_file.is_file()
    assert (secret_file.stat().st_mode & 0o777) == 0o600
    assert json.loads(secret_file.read_text())["password"] == "p"


@pytest.mark.unit
def test_migrate_is_idempotent(isolated_xdg: Path, clean_cwd: Path) -> None:
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    configs.mkdir(parents=True, exist_ok=True)
    (configs / "legacy.json").write_text(
        json.dumps({"src_name": "disc", "src_args": {"path": "/data/x"}})
    )
    migrate_addresses.main(argparse.Namespace(write=True, only=[]))
    # A second run must be a no-op (nothing left to migrate).
    rc = migrate_addresses.main(argparse.Namespace(write=True, only=[]))
    assert rc == 0


@pytest.mark.unit
def test_only_filter_scopes_migration(isolated_xdg: Path, clean_cwd: Path) -> None:
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    configs.mkdir(parents=True, exist_ok=True)
    (configs / "a.json").write_text(json.dumps({"src_name": "disc", "src_args": {"path": "/a"}}))
    (configs / "b.json").write_text(json.dumps({"src_name": "disc", "src_args": {"path": "/b"}}))
    migrate_addresses.main(argparse.Namespace(write=True, only=["a"]))
    # ``a`` moved, ``b`` is untouched.
    a = json.loads((configs / "a.json").read_text())
    b = json.loads((configs / "b.json").read_text())
    assert "connection" in a
    assert "src_name" in b
