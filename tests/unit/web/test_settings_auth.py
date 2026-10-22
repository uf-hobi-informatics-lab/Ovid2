"""Tests for authentication settings models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ovid2.web.models import AuthProviderConfig, GlobalSettings, SettingsAuth


@pytest.mark.unit
def test_global_settings_auth_defaults() -> None:
    """GlobalSettings should have auth sub-model with correct defaults."""
    settings = GlobalSettings()
    assert settings.auth.enabled is False
    assert settings.auth.idle_ttl_minutes == 45
    assert settings.auth.absolute_ttl_minutes == 480


@pytest.mark.unit
def test_bootstrap_admin_emails_normalization() -> None:
    """bootstrap_admin_emails should lowercase, strip, and drop empties."""
    auth = SettingsAuth(bootstrap_admin_emails=[" Foo@EXAMPLE.org ", "", "BAR@EXAMPLE.COM"])
    assert auth.bootstrap_admin_emails == ["foo@example.org", "bar@example.com"]


@pytest.mark.unit
def test_provider_dict_parses() -> None:
    """A providers dict with a google block should parse into AuthProviderConfig."""
    providers_dict = {
        "google": {
            "display_name": "Google",
            "issuer": "https://accounts.google.com",
            "client_id": "12345.apps.googleusercontent.com",
            "scopes": ["openid", "email"],
            "enabled": True,
        }
    }
    auth = SettingsAuth(providers=providers_dict)
    assert "google" in auth.providers
    provider = auth.providers["google"]
    assert isinstance(provider, AuthProviderConfig)
    assert provider.display_name == "Google"
    assert provider.issuer == "https://accounts.google.com"
    assert provider.client_id == "12345.apps.googleusercontent.com"
    assert provider.scopes == ["openid", "email"]
    assert provider.enabled is True


@pytest.mark.unit
def test_provider_missing_client_id_raises() -> None:
    """A provider missing client_id should raise ValidationError."""
    providers_dict = {
        "google": {
            "display_name": "Google",
            "issuer": "https://accounts.google.com",
            # client_id is missing
            "scopes": ["openid", "email"],
            "enabled": True,
        }
    }
    with pytest.raises(ValidationError) as exc_info:
        SettingsAuth(providers=providers_dict)
    assert "client_id" in str(exc_info.value)
