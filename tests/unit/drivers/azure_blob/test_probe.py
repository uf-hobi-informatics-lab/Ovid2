"""Unit tests for :mod:`ovid2.drivers.azure_blob.probe`.

Locks the invariants of :func:`probe_auth_from_config`:

- When ``azure-storage-blob`` is not installed the probe returns
  ``code='skipped'`` — never raises ``ImportError``.
- When ``account_name`` is missing (and no connection string is set)
  the probe returns ``code='config'``.
- When AAD-based modes need :mod:`azure.identity` and that package is
  missing the probe returns ``code='skipped'``.
- ``container_default`` triggers ``get_container_properties()``; the
  fallback path uses ``get_account_information()``.
- Auth-hint substrings downgrade the failure code to ``'auth'``.
- Every credential shape flows through to the constructed client
  verbatim.

The Azure SDK is optional: this suite installs minimal fakes for
``azure.storage.blob`` / ``azure.identity`` in :data:`sys.modules`
via ``monkeypatch.setitem`` so the tests run regardless of whether
the ``ovid2[azure]`` extra is installed on the host.
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from ovid2.drivers.azure_blob.probe import (
    _AUTH_HINTS,
    _PROBE_TIMEOUT_SECONDS,
    probe_auth_from_config,
)


def _install_fake_azure(
    monkeypatch: pytest.MonkeyPatch,
    *,
    identity: bool = True,
) -> dict[str, MagicMock]:
    """Register minimal fakes under ``azure.*`` in ``sys.modules``.

    Returns:
        Handles the tests introspect: the ``BlobServiceClient`` class
        mock, its ``from_connection_string`` classmethod mock, and the
        two credential class mocks.
    """
    azure_pkg = types.ModuleType("azure")
    storage_pkg = types.ModuleType("azure.storage")
    blob_pkg = types.ModuleType("azure.storage.blob")

    blob_service_client_cls = MagicMock(name="BlobServiceClient")
    blob_service_client_cls.from_connection_string = MagicMock(
        name="BlobServiceClient.from_connection_string",
    )
    blob_pkg.BlobServiceClient = blob_service_client_cls  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "azure", azure_pkg)
    monkeypatch.setitem(sys.modules, "azure.storage", storage_pkg)
    monkeypatch.setitem(sys.modules, "azure.storage.blob", blob_pkg)

    result: dict[str, MagicMock] = {"BlobServiceClient": blob_service_client_cls}

    if identity:
        identity_pkg = types.ModuleType("azure.identity")
        default_credential_cls = MagicMock(name="DefaultAzureCredential")
        client_secret_credential_cls = MagicMock(name="ClientSecretCredential")
        identity_pkg.DefaultAzureCredential = default_credential_cls  # type: ignore[attr-defined]
        identity_pkg.ClientSecretCredential = client_secret_credential_cls  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "azure.identity", identity_pkg)
        result["DefaultAzureCredential"] = default_credential_cls
        result["ClientSecretCredential"] = client_secret_credential_cls

    return result


class TestSkipsWhenSdkAbsent:
    """A host without the Azure SDK gets a clean ``skipped`` result."""

    def test_missing_azure_storage_blob(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Guarantee the import fails: pre-populate sys.modules with
        # ``azure.storage.blob = None`` so the ``import`` statement
        # raises ``ImportError``.
        for mod in ("azure", "azure.storage", "azure.storage.blob"):
            monkeypatch.setitem(sys.modules, mod, None)
        result = probe_auth_from_config({"account_name": "acct"}, {})
        assert result.ok is False
        assert result.code == "skipped"
        assert "azure-storage-blob" in (result.message or "")

    def test_missing_azure_identity_for_managed_identity(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_azure(monkeypatch, identity=False)
        # Force ``import azure.identity`` to fail.
        monkeypatch.setitem(sys.modules, "azure.identity", None)
        result = probe_auth_from_config({"account_name": "acct"}, {})
        assert result.ok is False
        assert result.code == "skipped"
        assert "azure-identity" in (result.message or "")

    def test_missing_azure_identity_for_service_principal(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_azure(monkeypatch, identity=False)
        monkeypatch.setitem(sys.modules, "azure.identity", None)
        result = probe_auth_from_config(
            {"account_name": "acct"},
            {"tenant_id": "T", "client_id": "C", "client_secret": "S"},
        )
        assert result.code == "skipped"


class TestConfigGuards:
    """Missing required config surfaces as ``code='config'``."""

    def test_missing_account_name_without_connection_string(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_azure(monkeypatch)
        result = probe_auth_from_config({}, {})
        assert result.ok is False
        assert result.code == "config"
        assert "account_name" in (result.message or "")

    def test_connection_string_bypasses_account_name_requirement(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handles = _install_fake_azure(monkeypatch)
        handles["BlobServiceClient"].from_connection_string.return_value = (
            _fake_ok_client()
        )
        result = probe_auth_from_config(
            {},
            {"connection_string": "DefaultEndpointsProtocol=https;"},
        )
        # No account_name → but connection_string carries the endpoint.
        assert result.code == "ok"


class TestSuccessfulProbes:
    """Happy-path probes return ``code='ok'``."""

    def test_container_default_triggers_get_container_properties(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handles = _install_fake_azure(monkeypatch)
        blob_client = _fake_ok_client()
        handles["BlobServiceClient"].return_value = blob_client
        result = probe_auth_from_config(
            {
                "account_name": "acct",
                "container_default": "landing",
                "endpoint_suffix": "core.windows.net",
            },
            {"account_key": "k"},
        )
        assert result.ok is True
        assert result.code == "ok"
        assert result.detail["container"] == "landing"
        assert result.detail["auth_mode"] == "account_key"
        # get_container_client was called with the container name.
        blob_client.get_container_client.assert_called_with("landing")

    def test_no_container_default_falls_back_to_get_account_info(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handles = _install_fake_azure(monkeypatch)
        blob_client = _fake_ok_client()
        handles["BlobServiceClient"].return_value = blob_client
        result = probe_auth_from_config(
            {"account_name": "acct"},
            {"account_key": "k"},
        )
        assert result.code == "ok"
        blob_client.get_account_information.assert_called_once()

    def test_default_chain_used_when_no_secrets(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handles = _install_fake_azure(monkeypatch)
        blob_client = _fake_ok_client()
        handles["BlobServiceClient"].return_value = blob_client
        result = probe_auth_from_config({"account_name": "acct"}, {})
        assert result.detail["auth_mode"] == "default_chain"
        handles["DefaultAzureCredential"].assert_called_once_with()


class TestFailureClassification:
    """Auth-hint substrings downgrade the failure code."""

    @pytest.mark.parametrize("hint", _AUTH_HINTS)
    def test_auth_hint_downgrades_to_auth(
        self, monkeypatch: pytest.MonkeyPatch, hint: str,
    ) -> None:
        handles = _install_fake_azure(monkeypatch)
        blob_client = _fake_failing_client(f"XYZ {hint} XYZ")
        handles["BlobServiceClient"].return_value = blob_client
        result = probe_auth_from_config(
            {"account_name": "acct"}, {"account_key": "k"},
        )
        assert result.ok is False
        assert result.code == "auth"

    def test_arbitrary_failure_is_unreachable(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handles = _install_fake_azure(monkeypatch)
        blob_client = _fake_failing_client("connection refused")
        handles["BlobServiceClient"].return_value = blob_client
        result = probe_auth_from_config(
            {"account_name": "acct"}, {"account_key": "k"},
        )
        assert result.code == "unreachable"

    def test_client_construction_failure_is_config(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handles = _install_fake_azure(monkeypatch)
        handles["BlobServiceClient"].side_effect = ValueError("bad url")
        result = probe_auth_from_config(
            {"account_name": "acct"}, {"account_key": "k"},
        )
        assert result.code == "config"


class TestTimeoutConstant:
    """The probe timeout is applied to every SDK call."""

    def test_probe_timeout_is_finite(self) -> None:
        assert 0 < _PROBE_TIMEOUT_SECONDS <= 60

    def test_get_container_properties_uses_timeout(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handles = _install_fake_azure(monkeypatch)
        blob_client = _fake_ok_client()
        handles["BlobServiceClient"].return_value = blob_client
        probe_auth_from_config(
            {"account_name": "acct", "container_default": "landing"},
            {"account_key": "k"},
        )
        container_client = blob_client.get_container_client.return_value
        container_client.get_container_properties.assert_called_with(
            timeout=_PROBE_TIMEOUT_SECONDS,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_ok_client() -> MagicMock:
    """Return a ``BlobServiceClient`` mock whose calls succeed."""
    client = MagicMock(name="BlobServiceClient-instance")
    client.url = "https://acct.blob.core.windows.net/"
    client.get_account_information.return_value = {"sku": "Standard_LRS"}
    container_client = MagicMock(name="ContainerClient-instance")
    container_client.get_container_properties.return_value = MagicMock(
        name="ContainerProperties",
    )
    client.get_container_client.return_value = container_client
    return client


def _fake_failing_client(message: str) -> MagicMock:
    """Return a ``BlobServiceClient`` mock whose calls raise."""

    class _Boom(Exception):
        pass

    client = MagicMock(name="BlobServiceClient-instance")
    client.url = "https://acct.blob.core.windows.net/"
    client.get_account_information.side_effect = _Boom(message)
    container_client = MagicMock(name="ContainerClient-instance")
    container_client.get_container_properties.side_effect = _Boom(message)
    client.get_container_client.return_value = container_client
    return client


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run with pytest.")
