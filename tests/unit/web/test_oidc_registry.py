"""Unit tests for the OIDC provider registry (Wave 2, auth core).

Exercises :mod:`ovid2.web.auth.oidc` with a stubbed ``SettingsAuth``
and real (test-local) secret files written under the isolated XDG
``secrets.d/``. No live network calls are made: providers are
registered with a discovery ``server_metadata_url`` that Authlib only
fetches at authorize time, which these tests never trigger.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from ovid2.web.auth import oidc
from ovid2.web.models import AuthProviderConfig, SettingsAuth


def _write_secret(xdg_root: Path, provider_key: str, client_secret: str) -> None:
    """Write ``secrets.d/auth_<provider_key>.json`` with mode 0o600."""
    secrets_dir = xdg_root / "config" / "ovid2" / "secrets.d"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    path = secrets_dir / f"{oidc.secret_name_for(provider_key)}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"client_secret": client_secret}, fh)
    os.chmod(path, 0o600)


def _provider(display_name: str, *, enabled: bool = True) -> AuthProviderConfig:
    """Build an AuthProviderConfig with sensible test defaults."""
    return AuthProviderConfig(
        display_name=display_name,
        issuer="https://idp.example.test/",
        client_id=f"{display_name.lower()}-client-id",
        scopes=["openid", "email", "profile"],
        enabled=enabled,
    )


@pytest.mark.unit
def test_two_enabled_providers_yield_two_clients(isolated_xdg: Path) -> None:
    """Two enabled providers with resolvable secrets → two registered clients."""
    _write_secret(isolated_xdg, "google", "google-secret")
    _write_secret(isolated_xdg, "microsoft", "microsoft-secret")
    auth = SettingsAuth(
        enabled=True,
        providers={
            "google": _provider("Google"),
            "microsoft": _provider("Microsoft"),
        },
    )

    registry = oidc.get_registry(auth)

    assert registry.create_client("google") is not None
    assert registry.create_client("microsoft") is not None

    enabled = oidc.list_enabled_providers(auth)
    assert {p.key for p in enabled} == {"google", "microsoft"}
    assert {p.display_name for p in enabled} == {"Google", "Microsoft"}


@pytest.mark.unit
def test_disabled_provider_absent_from_enabled_list(isolated_xdg: Path) -> None:
    """A provider with enabled=false is not returned and not registered."""
    _write_secret(isolated_xdg, "google", "google-secret")
    _write_secret(isolated_xdg, "microsoft", "microsoft-secret")
    auth = SettingsAuth(
        enabled=True,
        providers={
            "google": _provider("Google", enabled=True),
            "microsoft": _provider("Microsoft", enabled=False),
        },
    )

    enabled = oidc.list_enabled_providers(auth)
    assert [p.key for p in enabled] == ["google"]

    registry = oidc.get_registry(auth)
    assert registry.create_client("google") is not None
    # A disabled provider was never registered.
    assert registry.create_client("microsoft") is None


@pytest.mark.unit
def test_missing_secret_provider_skipped_with_warning(
    isolated_xdg: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An enabled provider whose secret file is missing is skipped (logged)."""
    # Only google has a secret; microsoft's secret file is absent.
    _write_secret(isolated_xdg, "google", "google-secret")
    auth = SettingsAuth(
        enabled=True,
        providers={
            "google": _provider("Google"),
            "microsoft": _provider("Microsoft"),
        },
    )

    with caplog.at_level(logging.WARNING, logger=oidc.logger.name):
        enabled = oidc.list_enabled_providers(auth)
        registry = oidc.get_registry(auth)

    # The provider missing its secret is absent from the enabled list.
    assert [p.key for p in enabled] == ["google"]
    # It was skipped (a warning was logged, not a crash).
    assert any(
        "microsoft" in rec.getMessage() and rec.levelno == logging.WARNING
        for rec in caplog.records
    )
    # The secret value is never logged.
    assert "google-secret" not in caplog.text
    # Only the resolvable provider produced a client.
    assert registry.create_client("google") is not None
    assert registry.create_client("microsoft") is None


@pytest.mark.unit
def test_secret_file_without_client_secret_key_skipped(
    isolated_xdg: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A secret file lacking the client_secret key is skipped with a warning."""
    secrets_dir = isolated_xdg / "config" / "ovid2" / "secrets.d"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    path = secrets_dir / f"{oidc.secret_name_for('google')}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"unrelated_key": "value"}, fh)
    os.chmod(path, 0o600)

    auth = SettingsAuth(enabled=True, providers={"google": _provider("Google")})

    with caplog.at_level(logging.WARNING, logger=oidc.logger.name):
        enabled = oidc.list_enabled_providers(auth)

    assert enabled == []
    assert any("google" in rec.getMessage() for rec in caplog.records)


@pytest.mark.unit
def test_server_metadata_url_strips_trailing_slash() -> None:
    """Issuer trailing slash is stripped before joining the discovery suffix."""
    assert (
        oidc._server_metadata_url("https://idp.example.test/")
        == "https://idp.example.test/.well-known/openid-configuration"
    )
    assert (
        oidc._server_metadata_url("https://idp.example.test")
        == "https://idp.example.test/.well-known/openid-configuration"
    )
