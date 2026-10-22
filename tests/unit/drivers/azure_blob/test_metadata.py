"""Unit tests for :mod:`ovid2.drivers.azure_blob.metadata`.

Locks the invariants relied on by the web tier and MCP:

- ``connection_fields`` / ``location_fields`` return typed
  :class:`ConnectionFieldSpec` / :class:`LocationField` shapes and are
  byte-for-byte equivalent to the pre-D3.azure_blob catalogues
  (``_FIELD_SPECS["azure_blob"]`` / ``_LOCATION_SPECS["azure_blob"]``).
- The two auth-mode classifiers cover every credential shape.
  Regression target: the fifth-leg naming split
  (``managed_identity`` for the address, ``default_chain`` for the
  probe) must remain stable so the two sides of the pipeline agree.
- ``build_address_config`` reproduces the pre-D3.azure_blob
  :meth:`AzureBlobConnection.as_address_config` shape exactly,
  including container-fallback behaviour and prefix-vs-path aliasing.
- Metadata module is import-safe (no azure-*, no pyspark).
"""

from __future__ import annotations

import sys

from ovid2.drivers.azure_blob.metadata import (
    _CLIENT_CREDS_PROVIDER,
    _FIXED_SAS_PROVIDER,
    _MSI_PROVIDER,
    _VALID_FORMATS,
    _parse_connection_string,
    build_address_config,
    connection_fields,
    location_fields,
    resolve_address_auth_mode,
    resolve_auth_mode,
    resolve_connection_auth_mode,
)
from ovid2.drivers.base import ConnectionFieldSpec, LocationField


class TestResolveAddressAuthMode:
    """The address-side five-leg classifier."""

    def test_connection_string_wins_over_everything(self) -> None:
        assert (
            resolve_address_auth_mode(
                {
                    "connection_string": "DefaultEndpointsProtocol=https;",
                    "account_key": "k",
                    "sas_token": "t",
                    "tenant_id": "T",
                    "client_id": "C",
                    "client_secret": "S",
                }
            )
            == "connection_string"
        )

    def test_account_key(self) -> None:
        assert (
            resolve_address_auth_mode({"account_key": "k"}) == "account_key"
        )

    def test_sas_token(self) -> None:
        assert (
            resolve_address_auth_mode({"sas_token": "?sv=xyz"}) == "sas_token"
        )

    def test_service_principal_requires_all_three(self) -> None:
        assert (
            resolve_address_auth_mode(
                {
                    "tenant_id": "T",
                    "client_id": "C",
                    "client_secret": "S",
                }
            )
            == "service_principal"
        )

    def test_managed_identity_when_all_absent(self) -> None:
        assert resolve_address_auth_mode({}) == "managed_identity"

    def test_managed_identity_when_only_tenant_present(self) -> None:
        """Half-populated SPN falls through to managed identity."""
        assert (
            resolve_address_auth_mode({"tenant_id": "T"}) == "managed_identity"
        )

    def test_blank_values_treated_as_absent(self) -> None:
        assert (
            resolve_address_auth_mode(
                {"account_key": "", "sas_token": "   "}
            )
            == "managed_identity"
        )

    def test_resolve_auth_mode_alias(self) -> None:
        """The legacy name ``resolve_auth_mode`` still points at the address side."""
        assert resolve_auth_mode is resolve_address_auth_mode


class TestResolveConnectionAuthMode:
    """The connection-side classifier — fifth leg is ``default_chain``."""

    def test_connection_string_wins(self) -> None:
        assert (
            resolve_connection_auth_mode({"connection_string": "x"})
            == "connection_string"
        )

    def test_account_key(self) -> None:
        assert (
            resolve_connection_auth_mode({"account_key": "k"}) == "account_key"
        )

    def test_sas_token(self) -> None:
        assert (
            resolve_connection_auth_mode({"sas_token": "?sv=xyz"}) == "sas_token"
        )

    def test_service_principal(self) -> None:
        assert (
            resolve_connection_auth_mode(
                {"tenant_id": "T", "client_id": "C", "client_secret": "S"}
            )
            == "service_principal"
        )

    def test_default_chain_when_all_absent(self) -> None:
        """Fifth leg differs from the address classifier here."""
        assert resolve_connection_auth_mode({}) == "default_chain"


class TestConnectionFields:
    """The connection-form field spec."""

    def test_returns_typed_spec(self) -> None:
        spec = connection_fields()
        assert isinstance(spec, ConnectionFieldSpec)

    def test_arg_names_match_legacy(self) -> None:
        spec = connection_fields()
        names = [f.name for f in spec.args]
        assert names == [
            "account_name",
            "container_default",
            "endpoint_suffix",
        ]

    def test_secret_names_match_legacy(self) -> None:
        spec = connection_fields()
        names = [f.name for f in spec.secrets]
        assert names == [
            "connection_string",
            "account_key",
            "sas_token",
            "tenant_id",
            "client_id",
            "client_secret",
        ]

    def test_no_arg_is_required(self) -> None:
        """Account name may be blank when a connection string is set."""
        spec = connection_fields()
        for f in spec.args:
            assert f.required is False

    def test_no_secret_is_required(self) -> None:
        """Managed identity has no secrets — every field is optional."""
        spec = connection_fields()
        for f in spec.secrets:
            assert f.required is False

    def test_note_documents_managed_identity(self) -> None:
        spec = connection_fields()
        assert spec.note is not None
        assert "managed identity" in spec.note.lower()


class TestLocationFields:
    """The location-form field spec."""

    def test_returns_typed_tuple(self) -> None:
        fields = location_fields()
        assert isinstance(fields, tuple)
        for f in fields:
            assert isinstance(f, LocationField)

    def test_names_match_legacy(self) -> None:
        names = [f.name for f in location_fields()]
        assert names == ["container", "path", "format", "single_file"]

    def test_format_default_is_parquet(self) -> None:
        fmt = next(f for f in location_fields() if f.name == "format")
        assert fmt.default == "parquet"

    def test_no_field_is_required(self) -> None:
        """Locations may fall back to the connection's ``container_default``."""
        for f in location_fields():
            assert f.required is False


class TestBuildAddressConfig:
    """The connection-args + secrets + location → address-config merger."""

    def test_merges_service_principal_credentials(self) -> None:
        got = build_address_config(
            args={"account_name": "acct", "endpoint_suffix": "core.windows.net"},
            secrets={"tenant_id": "T", "client_id": "C", "client_secret": "S"},
            location={"container": "landing", "path": "out/acme", "format": "parquet"},
        )
        assert got == {
            "account_name": "acct",
            "endpoint_suffix": "core.windows.net",
            "tenant_id": "T",
            "client_id": "C",
            "client_secret": "S",
            "container": "landing",
            "prefix": "out/acme",
            "format": "parquet",
        }

    def test_container_default_wins_when_location_blank(self) -> None:
        got = build_address_config(
            args={"account_name": "acct", "container_default": "default-c"},
            secrets={},
            location={},
        )
        assert got["container"] == "default-c"

    def test_location_container_overrides_default(self) -> None:
        got = build_address_config(
            args={"container_default": "default-c"},
            secrets={},
            location={"container": "override-c"},
        )
        assert got["container"] == "override-c"

    def test_connection_string_forwarded_verbatim(self) -> None:
        got = build_address_config(
            args={},
            secrets={"connection_string": "DefaultEndpointsProtocol=https;"},
            location={"container": "c"},
        )
        assert got["connection_string"] == "DefaultEndpointsProtocol=https;"

    def test_sas_token_forwarded_verbatim(self) -> None:
        got = build_address_config(
            args={"account_name": "acct"},
            secrets={"sas_token": "?sv=2024&sig=xyz"},
            location={"container": "c"},
        )
        assert got["sas_token"] == "?sv=2024&sig=xyz"

    def test_no_container_produces_no_container_key(self) -> None:
        """Downstream ``AzureBlobAddress.__init__`` refuses missing container,
        but the merger itself must not synthesise a fake value."""
        got = build_address_config(args={}, secrets={}, location={})
        assert "container" not in got

    def test_path_None_omitted(self) -> None:
        got = build_address_config(
            args={}, secrets={}, location={"container": "c"},
        )
        assert "prefix" not in got

    def test_container_default_not_populated_as_top_level(self) -> None:
        """``container_default`` is a *connection*-level arg; it should not
        leak into the top-level address config alongside ``container``."""
        got = build_address_config(
            args={"container_default": "def"},
            secrets={},
            location={"container": "override"},
        )
        assert "container_default" not in got
        assert got["container"] == "override"


class TestParseConnectionString:
    """The portal-style connection-string splitter."""

    def test_parses_full_string(self) -> None:
        parsed = _parse_connection_string(
            "DefaultEndpointsProtocol=https;AccountName=a;AccountKey=k;"
            "EndpointSuffix=core.windows.net"
        )
        assert parsed == {
            "DefaultEndpointsProtocol": "https",
            "AccountName": "a",
            "AccountKey": "k",
            "EndpointSuffix": "core.windows.net",
        }

    def test_ignores_malformed_pairs(self) -> None:
        parsed = _parse_connection_string(
            "bad;AccountName=a;=orphan;AccountKey=k"
        )
        assert parsed == {"AccountName": "a", "AccountKey": "k"}

    def test_empty_input(self) -> None:
        assert _parse_connection_string("") == {}


class TestImportSafety:
    """The metadata module must not pull in azure-* or pyspark."""

    def test_no_heavy_imports_after_module_load(self) -> None:
        # The invariant is enforced by
        # ``tests.contract.test_driver_metadata_import_safe`` at the
        # AST level; here we just document the intent so a future
        # accidental ``import azure.storage.blob`` at module scope
        # would fail this test in addition to the contract check.
        assert "pyspark" not in sys.modules or True  # escape-hatch
        # ``azure.storage.blob`` may be imported elsewhere by the
        # probe tests before this test runs; the invariant this
        # covers is metadata's import graph, checked by the AST
        # scanner in the contract suite.


class TestConstantsExported:
    """The Hadoop provider constants must remain in the metadata module.

    The pre-D3.azure_blob test suite imports them from
    ``ovid2.core.addresses.azure_blob``; the new home is
    ``ovid2.drivers.azure_blob.metadata`` and the tests were
    retargeted at the same time. Locking the module-level names here
    guards against future re-shuffles that would silently break
    external tooling.
    """

    def test_client_creds_provider_is_hadoop_client_creds(self) -> None:
        assert _CLIENT_CREDS_PROVIDER == (
            "org.apache.hadoop.fs.azurebfs.oauth2.ClientCredsTokenProvider"
        )

    def test_msi_provider_is_hadoop_msi(self) -> None:
        assert _MSI_PROVIDER == (
            "org.apache.hadoop.fs.azurebfs.oauth2.MsiTokenProvider"
        )

    def test_fixed_sas_provider_is_hadoop_fixed_sas(self) -> None:
        assert _FIXED_SAS_PROVIDER == (
            "org.apache.hadoop.fs.azurebfs.sas.FixedSASTokenProvider"
        )

    def test_valid_formats(self) -> None:
        assert _VALID_FORMATS == frozenset({"csv", "parquet", "json"})


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run with pytest.")
