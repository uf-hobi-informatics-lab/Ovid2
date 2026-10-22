"""Unit tests for the ``ovid2 web`` recovery CLI subcommands (Todo 20)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from ovid2.web import cli
from ovid2.web import db as web_db
from ovid2.web.auth import breakglass
from ovid2.web.auth.roles import Role
from ovid2.web.services import users_svc


def _audit_actions() -> list[str]:
    """Return every recorded ``web_audit`` action string."""
    with web_db.connect() as conn:
        rows = conn.execute("SELECT action FROM web_audit").fetchall()
    return [row["action"] for row in rows]


@pytest.mark.unit
def test_grant_admin_creates_and_activates(isolated_xdg: Path) -> None:
    web_db.ensure_schema()
    rc = cli.grant_admin(argparse.Namespace(email="ops@example.org"))
    assert rc == 0
    assert users_svc.count_active_admins() == 1

    user = cli._find_user_by_email("ops@example.org")
    assert user is not None
    assert user["role"] == Role.SYSTEM_ADMIN.value
    assert user["status"] == "active"
    assert user["issuer"] == "ovid2:recovery"
    assert "user.activate" in _audit_actions()


@pytest.mark.unit
def test_list_admins_prints_the_admin(
    isolated_xdg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    web_db.ensure_schema()
    cli.grant_admin(argparse.Namespace(email="boss@example.org"))
    capsys.readouterr()

    rc = cli.list_admins(argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "boss@example.org" in out


@pytest.mark.unit
def test_set_role_changes_role(isolated_xdg: Path) -> None:
    web_db.ensure_schema()
    cli.grant_admin(argparse.Namespace(email="a@example.org"))
    cli.grant_admin(argparse.Namespace(email="b@example.org"))

    rc = cli.set_role(argparse.Namespace(email="b@example.org", role="viewer"))
    assert rc == 0
    user = cli._find_user_by_email("b@example.org")
    assert user is not None
    assert user["role"] == Role.VIEWER.value
    assert "user.set_role" in _audit_actions()


@pytest.mark.unit
def test_set_role_invalid_role_returns_2(isolated_xdg: Path) -> None:
    web_db.ensure_schema()
    cli.grant_admin(argparse.Namespace(email="a@example.org"))
    rc = cli.set_role(argparse.Namespace(email="a@example.org", role="wizard"))
    assert rc == 2


@pytest.mark.unit
def test_set_role_last_admin_guard_returns_1(isolated_xdg: Path) -> None:
    web_db.ensure_schema()
    cli.grant_admin(argparse.Namespace(email="only@example.org"))
    rc = cli.set_role(argparse.Namespace(email="only@example.org", role="viewer"))
    assert rc == 1


@pytest.mark.unit
def test_set_role_missing_user_returns_1(isolated_xdg: Path) -> None:
    web_db.ensure_schema()
    rc = cli.set_role(argparse.Namespace(email="ghost@example.org", role="viewer"))
    assert rc == 1


@pytest.mark.unit
def test_reset_break_glass_stores_verifiable_hash(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    breakglass._reset_lockout()
    web_db.ensure_schema()
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "correct horse battery staple")

    rc = cli.reset_break_glass(argparse.Namespace())
    assert rc == 0
    assert breakglass.is_configured() is True
    assert breakglass.verify("correct horse battery staple") is True
    assert "breakglass.set" in _audit_actions()


@pytest.mark.unit
def test_reset_break_glass_never_prints_password(
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    breakglass._reset_lockout()
    web_db.ensure_schema()
    secret = "do-not-echo-me-42"
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": secret)

    cli.reset_break_glass(argparse.Namespace())
    out = capsys.readouterr()
    assert secret not in out.out
    assert secret not in out.err


@pytest.mark.unit
def test_register_default_entry_is_server(isolated_xdg: Path) -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    cli.register(subparsers)
    args = parser.parse_args(["web"])
    assert args._entry is cli.main
    assert args.web_action is None
