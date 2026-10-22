"""OIDC provider registry (Wave 2, auth core).

Builds an Authlib :class:`~authlib.integrations.starlette_client.OAuth`
registry from ``settings.auth.providers``. Each enabled provider is
registered with an OIDC *discovery* URL
(``<issuer>/.well-known/openid-configuration``) — no Google/Microsoft
endpoints are ever hardcoded here — and PKCE (S256) is enabled via
``client_kwargs``.

Client secrets live in ``secrets.d/`` and are referenced by provider
key. They are resolved **lazily** — only when :func:`get_registry` (or
:func:`list_enabled_providers`) runs, never at module import time — and
are **never logged**. The read path is
:func:`ovid2.config.secrets.load_secret`; the web-tier
:mod:`ovid2.web.services.secrets_svc` deliberately returns key *names*
only, so it cannot be used to obtain a value.

A provider whose secret file is missing, unreadable, or lacks the
``client_secret`` key is *skipped* with a ``logger.warning`` — it will
not appear in :func:`list_enabled_providers` and no client is
registered for it. This keeps a half-configured provider from crashing
the whole login page.

This module is part of the web tier and must not import any pipeline
module (``ovid2.core``, ``ovid2.transformations``, ``ovid2.formatters``,
``ovid2.mappers``, ``pipelines``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from authlib.integrations.starlette_client import OAuth

from ovid2.config.secrets import (
    SecretNotFoundError,
    SecretPermissionError,
    load_secret,
)
from ovid2.web.models import AuthProviderConfig, SettingsAuth

logger = logging.getLogger(__name__)

__all__ = [
    "EnabledProvider",
    "get_registry",
    "list_enabled_providers",
    "secret_name_for",
]

#: JSON key inside each provider secret file that holds the client secret.
_CLIENT_SECRET_KEY = "client_secret"  # noqa: S105 — JSON key name, not a secret value


@dataclass(frozen=True)
class EnabledProvider:
    """A provider that is enabled and has a resolvable client secret.

    Attributes:
        key: The provider key (the dict key under
            ``settings.auth.providers``), e.g. ``"google"``.
        display_name: Human-readable label for the login button.
    """

    key: str
    display_name: str


def secret_name_for(provider_key: str) -> str:
    """Return the secrets.d logical name that holds a provider's secret.

    Args:
        provider_key: The provider key from ``settings.auth.providers``.

    Returns:
        The logical secret name, e.g. ``"auth_google"``. The on-disk
        file is ``secrets.d/auth_<provider_key>.json`` with a
        ``client_secret`` key.
    """
    return f"auth_{provider_key}"


def _resolve_client_secret(provider_key: str) -> str | None:
    """Resolve one provider's client secret lazily, or ``None``.

    Reads ``secrets.d/auth_<provider_key>.json`` via
    :func:`ovid2.config.secrets.load_secret` and returns its
    ``client_secret`` value. Any failure (missing file, permissive
    mode, missing/blank key, malformed JSON) is logged at ``warning``
    and returns ``None`` so the caller can skip the provider. The
    secret value itself is never logged.

    Args:
        provider_key: The provider key from ``settings.auth.providers``.

    Returns:
        The client secret string, or ``None`` when it cannot be
        resolved.
    """
    secret_name = secret_name_for(provider_key)
    try:
        payload = load_secret(secret_name)
    except SecretNotFoundError:
        logger.warning(
            "OIDC provider %r skipped: secret file %r not found under secrets.d/",
            provider_key,
            secret_name,
        )
        return None
    except SecretPermissionError:
        logger.warning(
            "OIDC provider %r skipped: secret file %r has unsafe (world-readable) "
            "permissions; run `chmod 600` on it",
            provider_key,
            secret_name,
        )
        return None
    except (OSError, ValueError) as exc:
        logger.warning(
            "OIDC provider %r skipped: secret file %r is unreadable (%s)",
            provider_key,
            secret_name,
            exc,
        )
        return None

    raw = payload.get(_CLIENT_SECRET_KEY)
    if not isinstance(raw, str) or not raw.strip():
        logger.warning(
            "OIDC provider %r skipped: secret %r is missing a non-empty %r key",
            provider_key,
            secret_name,
            _CLIENT_SECRET_KEY,
        )
        return None
    return raw


def _server_metadata_url(issuer: str) -> str:
    """Return the OIDC discovery URL for an issuer.

    The trailing slash on ``issuer`` is stripped before joining so we
    never emit a double slash. No provider-specific endpoints are
    hardcoded — discovery does the rest.

    Args:
        issuer: The OIDC issuer URL from the provider config.

    Returns:
        ``<issuer>/.well-known/openid-configuration``.
    """
    return f"{issuer.rstrip('/')}/.well-known/openid-configuration"


def _client_kwargs(provider: AuthProviderConfig) -> dict[str, str]:
    """Return the Authlib ``client_kwargs`` for a provider (scopes + PKCE)."""
    return {
        "scope": " ".join(provider.scopes),
        "code_challenge_method": "S256",
    }


def _iter_resolvable_providers(
    auth: SettingsAuth,
) -> list[tuple[str, AuthProviderConfig, str]]:
    """Yield ``(key, config, secret)`` for every usable provider.

    A provider is usable when it is enabled *and* its client secret is
    resolvable. Disabled providers are silently ignored; enabled
    providers with an unresolvable secret are skipped with a warning by
    :func:`_resolve_client_secret`.

    Args:
        auth: The authentication settings block.

    Returns:
        A list of ``(provider_key, provider_config, client_secret)``
        triples, in ``providers`` insertion order.
    """
    resolved: list[tuple[str, AuthProviderConfig, str]] = []
    for key, provider in auth.providers.items():
        if not provider.enabled:
            continue
        secret = _resolve_client_secret(key)
        if secret is None:
            continue
        resolved.append((key, provider, secret))
    return resolved


def get_registry(auth: SettingsAuth) -> OAuth:
    """Build an Authlib registry with one client per usable provider.

    Client secrets are resolved lazily here (never at import time) and
    are never logged. Providers that are disabled, or whose secret
    cannot be resolved, are omitted from the returned registry.

    Args:
        auth: The authentication settings block
            (``settings.auth``).

    Returns:
        An :class:`~authlib.integrations.starlette_client.OAuth`
        instance holding one registered client per usable provider.
    """
    oauth = OAuth()
    for key, provider, secret in _iter_resolvable_providers(auth):
        oauth.register(
            name=key,
            client_id=provider.client_id,
            client_secret=secret,
            server_metadata_url=_server_metadata_url(provider.issuer),
            client_kwargs=_client_kwargs(provider),
        )
    return oauth


def list_enabled_providers(auth: SettingsAuth) -> list[EnabledProvider]:
    """Return the providers that are enabled and have a resolvable secret.

    This is the list the login page renders buttons for. A provider
    that is ``enabled=false`` — or that is enabled but whose secret
    file is missing/unreadable — is absent from the result.

    Args:
        auth: The authentication settings block
            (``settings.auth``).

    Returns:
        A list of :class:`EnabledProvider` in ``providers`` insertion
        order.
    """
    return [
        EnabledProvider(key=key, display_name=provider.display_name)
        for key, provider, _secret in _iter_resolvable_providers(auth)
    ]
