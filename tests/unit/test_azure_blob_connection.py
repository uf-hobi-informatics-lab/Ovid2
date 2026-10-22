"""Unit tests for :class:`ovid2.drivers.azure_blob.AzureBlobConnection` (legacy shim).

Preserved from the pre-D3.azure_blob era; the connection class now
lives at :mod:`ovid2.drivers.azure_blob.legacy` (re-exported from
:mod:`ovid2.drivers.azure_blob`) and delegates its probe to
:func:`ovid2.drivers.azure_blob.probe.probe_auth_from_config`. Every
test value survives byte-identical after the import-path retarget.

The Azure SDK is optional: this suite mocks the ``BlobServiceClient``
+ ``azure.identity`` surfaces so it runs regardless of whether the
``ovid2[azure]`` extra is installed.
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from ovid2.drivers.azure_blob import AzureBlobConnection

# ---------------------------------------------------------------------------
# Helpers: install fake ``azure.storage.blob`` / ``azure.identity`` modules.
# ---------------------------------------------------------------------------


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

    handles: dict[str, MagicMock] = {
        "BlobServiceClient": blob_service_client_cls,
        "from_connection_string": blob_service_client_cls.from_connection_string,
    }

    if identity:
        identity_pkg = types.ModuleType("azure.identity")
        client_secret_credential_cls = MagicMock(name="ClientSecretCredential")
        default_credential_cls = MagicMock(name="DefaultAzureCredential")
        identity_pkg.ClientSecretCredential = client_secret_credential_cls  # type: ignore[attr-defined]
        identity_pkg.DefaultAzureCredential = default_credential_cls  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "azure.identity", identity_pkg)
        handles["ClientSecretCredential"] = client_secret_credential_cls
        handles["DefaultAzureCredential"] = default_credential_cls

    return handles


def _make_client(**probe_returns: Any) -> MagicMock:
    """Return a ``BlobServiceClient`` mock with configured probe results."""
    client = MagicMock(name="BlobServiceClientInstance")
    client.url = probe_returns.get("url", "https://acct.blob.core.windows.net")
    if "container_props" in probe_returns:
        container_client = MagicMock(name="ContainerClient")
        container_client.get_container_properties.return_value = probe_returns["container_props"]
        client.get_container_client.return_value = container_client
    if "account_info" in probe_returns:
        client.get_account_information.return_value = probe_returns["account_info"]
    if "probe_exc" in probe_returns:
        client.get_account_information.side_effect = probe_returns["probe_exc"]
        client.get_container_client.side_effect = probe_returns["probe_exc"]
    return client


# ---------------------------------------------------------------------------
# _auth_mode selection
# ---------------------------------------------------------------------------


class TestAuthModeSelection:
    """Verify the precedence order documented in the module docstring."""

    def test_connection_string_wins(self) -> None:
        conn = AzureBlobConnection(
            args={"account_name": "acct"},
            secrets={
                "connection_string": "DefaultEndpointsProtocol=https;AccountName=x;AccountKey=y",
                "account_key": "should-be-ignored",
                "sas_token": "should-be-ignored",
                "tenant_id": "t",
                "client_id": "c",
                "client_secret": "s",
            },
        )
        assert conn._auth_mode() == "connection_string"

    def test_account_key_next(self) -> None:
        conn = AzureBlobConnection(
            args={"account_name": "acct"},
            secrets={"account_key": "k", "sas_token": "s"},
        )
        assert conn._auth_mode() == "account_key"

    def test_sas_token_next(self) -> None:
        conn = AzureBlobConnection(
            args={"account_name": "acct"},
            secrets={"sas_token": "?sv=…"},
        )
        assert conn._auth_mode() == "sas_token"

    def test_service_principal_requires_all_three(self) -> None:
        conn = AzureBlobConnection(
            args={"account_name": "acct"},
            secrets={"tenant_id": "t", "client_id": "c", "client_secret": "s"},
        )
        assert conn._auth_mode() == "service_principal"

    def test_partial_spn_falls_through_to_default_chain(self) -> None:
        conn = AzureBlobConnection(
            args={"account_name": "acct"},
            secrets={"tenant_id": "t", "client_id": "c"},  # missing client_secret
        )
        assert conn._auth_mode() == "default_chain"

    def test_no_secrets_uses_default_chain(self) -> None:
        conn = AzureBlobConnection(args={"account_name": "acct"})
        assert conn._auth_mode() == "default_chain"


# ---------------------------------------------------------------------------
# test() — skipped / config paths (no SDK / bad args)
# ---------------------------------------------------------------------------


class TestProbeSkipped:
    """Every path that must return ``code='skipped'`` cleanly."""

    def test_missing_azure_storage_blob(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Do NOT install the fake module: import must fail.
        for mod in list(sys.modules):
            if mod.startswith("azure"):
                monkeypatch.delitem(sys.modules, mod, raising=False)
        # Belt-and-braces: force the import to fail even if the wheel
        # is present in the environment.
        original_import = (
            __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__
        )

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.startswith("azure"):
                raise ImportError(f"forced miss for {name}")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", fake_import)

        conn = AzureBlobConnection(args={"account_name": "acct"})
        result = conn.test()
        assert result.ok is False
        assert result.code == "skipped"
        assert "azure-storage-blob" in result.message

    def test_missing_azure_identity_for_aad_mode(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Install azure.storage.blob but NOT azure.identity.
        _install_fake_azure(monkeypatch, identity=False)
        conn = AzureBlobConnection(args={"account_name": "acct"})  # default_chain
        result = conn.test()
        assert result.ok is False
        assert result.code == "skipped"
        assert "azure-identity" in result.message


class TestProbeConfig:
    """Cases that must return ``code='config'`` (bad inputs, no probe)."""

    def test_missing_account_name_without_connection_string(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_fake_azure(monkeypatch)
        conn = AzureBlobConnection(args={}, secrets={"account_key": "k"})
        result = conn.test()
        assert result.ok is False
        assert result.code == "config"
        assert "account_name" in result.message

    def test_connection_string_allows_missing_account_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handles = _install_fake_azure(monkeypatch)
        handles["from_connection_string"].return_value = _make_client(
            account_info={"sku_name": "Standard_LRS"}
        )
        conn = AzureBlobConnection(
            args={},
            secrets={
                "connection_string": "DefaultEndpointsProtocol=https;AccountName=x;AccountKey=y"
            },
        )
        result = conn.test()
        assert result.ok is True
        assert result.code == "ok"
        assert result.detail["auth_mode"] == "connection_string"

    def test_client_construction_valueerror_maps_to_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handles = _install_fake_azure(monkeypatch)
        handles["BlobServiceClient"].side_effect = ValueError("bad account_url")
        conn = AzureBlobConnection(args={"account_name": "acct"}, secrets={"account_key": "k"})
        result = conn.test()
        assert result.ok is False
        assert result.code == "config"
        assert result.detail["auth_mode"] == "account_key"


# ---------------------------------------------------------------------------
# test() — ok path (mocked SDK returns cleanly)
# ---------------------------------------------------------------------------


class TestProbeOk:
    """Happy paths through each auth mode."""

    def test_account_key_probes_account_info_by_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handles = _install_fake_azure(monkeypatch)
        client = _make_client(account_info={"sku_name": "Standard_LRS"})
        handles["BlobServiceClient"].return_value = client

        conn = AzureBlobConnection(
            args={"account_name": "acct", "endpoint_suffix": "core.windows.net"},
            secrets={"account_key": "abc"},
        )
        result = conn.test()

        assert result.ok is True
        assert result.code == "ok"
        assert result.detail["auth_mode"] == "account_key"
        # BlobServiceClient(account_url=…, credential='abc') was called
        # with the account key as the credential.
        handles["BlobServiceClient"].assert_called_once()
        kwargs = handles["BlobServiceClient"].call_args.kwargs
        assert kwargs["account_url"] == "https://acct.blob.core.windows.net"
        assert kwargs["credential"] == "abc"
        client.get_account_information.assert_called_once()
        client.get_container_client.assert_not_called()

    def test_container_default_switches_probe_to_container_properties(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handles = _install_fake_azure(monkeypatch)
        client = _make_client(container_props={"name": "landing"})
        handles["BlobServiceClient"].return_value = client

        conn = AzureBlobConnection(
            args={"account_name": "acct", "container_default": "landing"},
            secrets={"sas_token": "?sv=…"},
        )
        result = conn.test()

        assert result.ok is True
        assert result.detail["container"] == "landing"
        assert result.detail["auth_mode"] == "sas_token"
        client.get_container_client.assert_called_once_with("landing")
        client.get_account_information.assert_not_called()

    def test_service_principal_builds_client_secret_credential(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handles = _install_fake_azure(monkeypatch)
        client = _make_client(account_info={})
        handles["BlobServiceClient"].return_value = client
        handles["ClientSecretCredential"].return_value = "the-credential"

        conn = AzureBlobConnection(
            args={"account_name": "acct"},
            secrets={"tenant_id": "t", "client_id": "c", "client_secret": "s"},
        )
        result = conn.test()

        assert result.ok is True
        assert result.detail["auth_mode"] == "service_principal"
        handles["ClientSecretCredential"].assert_called_once_with(
            tenant_id="t",
            client_id="c",
            client_secret="s",
        )
        kwargs = handles["BlobServiceClient"].call_args.kwargs
        assert kwargs["credential"] == "the-credential"

    def test_default_chain_when_no_secrets(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handles = _install_fake_azure(monkeypatch)
        client = _make_client(account_info={})
        handles["BlobServiceClient"].return_value = client
        handles["DefaultAzureCredential"].return_value = "chain"

        conn = AzureBlobConnection(args={"account_name": "acct"})
        result = conn.test()

        assert result.ok is True
        assert result.detail["auth_mode"] == "default_chain"
        handles["DefaultAzureCredential"].assert_called_once_with()


# ---------------------------------------------------------------------------
# test() — error classification
# ---------------------------------------------------------------------------


class TestProbeErrors:
    """Verify the ``auth`` vs ``unreachable`` split."""

    @pytest.mark.parametrize(
        "message",
        [
            "AuthenticationFailed: Server failed to authenticate the request.",
            "AuthorizationFailure",
            "InvalidAuthenticationInfo: Signature not found.",
            "AADSTS7000215: Invalid client secret",
            # RBAC / policy denials — the caller *did* authenticate,
            # the service just refused the operation. These belong
            # under ``auth`` even though the SDK error type is the
            # same generic HttpResponseError as a transport failure.
            "AuthorizationPermissionMismatch: This request is not authorized to perform this operation using this permission.",
            "AuthorizationSourceIPMismatch: This request is not authorized to perform this operation using this source IP.",
            "AuthorizationProtocolMismatch: This request is not authorized to perform this operation using this protocol.",
            "AuthorizationResourceTypeMismatch: This request is not authorized to perform this operation using this resource type.",
        ],
    )
    def test_auth_hints_map_to_auth_code(
        self,
        monkeypatch: pytest.MonkeyPatch,
        message: str,
    ) -> None:
        handles = _install_fake_azure(monkeypatch)
        handles["BlobServiceClient"].return_value = _make_client(
            probe_exc=RuntimeError(message),
        )
        conn = AzureBlobConnection(
            args={"account_name": "acct"},
            secrets={"account_key": "k"},
        )
        result = conn.test()
        assert result.ok is False
        assert result.code == "auth"

    def test_unrelated_failures_map_to_unreachable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handles = _install_fake_azure(monkeypatch)
        handles["BlobServiceClient"].return_value = _make_client(
            probe_exc=OSError("Name or service not known"),
        )
        conn = AzureBlobConnection(
            args={"account_name": "acct"},
            secrets={"account_key": "k"},
        )
        result = conn.test()
        assert result.ok is False
        assert result.code == "unreachable"


# ---------------------------------------------------------------------------
# as_address_config
# ---------------------------------------------------------------------------


class TestAsAddressConfig:
    """The legacy-address bridge merges connection + location fields."""

    def test_forwards_every_populated_secret(self) -> None:
        conn = AzureBlobConnection(
            args={"account_name": "acct", "endpoint_suffix": "core.windows.net"},
            secrets={
                "account_key": "key",
                "sas_token": "sas",
                "tenant_id": "t",
                "client_id": "c",
                "client_secret": "s",
                "connection_string": "cs",
            },
        )
        merged = conn.as_address_config({"container": "landing", "path": "/x", "format": "parquet"})
        assert merged["account_name"] == "acct"
        assert merged["endpoint_suffix"] == "core.windows.net"
        for k in (
            "account_key",
            "sas_token",
            "tenant_id",
            "client_id",
            "client_secret",
            "connection_string",
        ):
            assert merged[k] is not None, f"expected {k} to survive merge"
        assert merged["container"] == "landing"
        assert merged["prefix"] == "/x"
        assert merged["format"] == "parquet"

    def test_container_default_used_when_location_has_none(self) -> None:
        conn = AzureBlobConnection(
            args={"account_name": "acct", "container_default": "default-c"},
            secrets={"account_key": "k"},
        )
        merged = conn.as_address_config({})
        assert merged["container"] == "default-c"
        assert "prefix" not in merged
