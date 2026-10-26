"""Import-safe metadata for the ``azure_blob`` driver.

Everything the web tier and MCP need to describe the Azure Blob
peripheral — form fields, location fields, address-config resolution.
Deliberately free of pyspark / azure-* imports so
:mod:`ovid2.drivers.azure_blob` can be enumerated by the web control
panel without pulling in ``azure-storage-blob`` or ``azure-identity``.

Contains:

- :data:`_VALID_FORMATS` — recognised on-disk formats.
- :data:`_CLIENT_CREDS_PROVIDER` / :data:`_MSI_PROVIDER` /
  :data:`_FIXED_SAS_PROVIDER` — Hadoop ``abfss`` OAuth / MSI / SAS
  provider class names (used by :mod:`ovid2.drivers.azure_blob.spark`;
  re-exported so any pre-D3.azure_blob importer of
  ``ovid2.core.addresses.azure_blob`` translates cleanly).
- :func:`resolve_address_auth_mode` — pure classifier over the
  credential dict, matches :class:`AzureBlobAddress`'s five-leg
  resolution (``connection_string`` > ``account_key`` > ``sas_token`` >
  ``service_principal`` > ``managed_identity``).
- :func:`resolve_connection_auth_mode` — connection-side classifier;
  identical to the address classifier except the fifth leg returns
  ``default_chain`` (matches
  :meth:`~ovid2.connections.azure_blob.AzureBlobConnection._auth_mode`).
- :func:`connection_fields` / :func:`location_fields` — the
  field-spec catalogue mirroring ``_FIELD_SPECS["azure_blob"]`` /
  ``_LOCATION_SPECS["azure_blob"]`` in
  :mod:`ovid2.connections.registry`.
- :func:`build_address_config` — the merger that
  :meth:`AzureBlobConnection.as_address_config` previously
  implemented inline. Kept as a module-level function so the
  driver's data-plane and the legacy shim share one shape.
- :func:`_parse_connection_string` — portal-connection-string
  splitter, re-exported so the pre-D3.azure_blob test suite's
  ``from ovid2.core.addresses.azure_blob import
  _parse_connection_string`` translates cleanly.
"""

from __future__ import annotations

from typing import Any

from ovid2.drivers.base import ConnectionFieldSpec, FieldSpec, LocationField

#: Recognised on-disk formats. Extension is tracked but not enforced —
#: unknown formats surface as a ``ValueError`` at read/write time so
#: mis-authored addresses fail loudly (matches the pre-refactor
#: behaviour).
_VALID_FORMATS: frozenset[str] = frozenset({"csv", "parquet", "json"})

#: Class name (dotted string) of the Hadoop ClientCredsTokenProvider —
#: service-principal (client-credentials) OAuth flow against Azure AD.
_CLIENT_CREDS_PROVIDER: str = "org.apache.hadoop.fs.azurebfs.oauth2.ClientCredsTokenProvider"

#: Class name of the Hadoop MsiTokenProvider (managed identity). This
#: is the default path on the Ovid host: the VM's system-assigned
#: managed identity has cross-account Blob Data Contributor rights, so
#: no secret material is needed at rest.
_MSI_PROVIDER: str = "org.apache.hadoop.fs.azurebfs.oauth2.MsiTokenProvider"

#: Class name of the fixed-SAS-token provider — used when a SAS token
#: is registered directly (rather than derived from a stored account
#: key).
_FIXED_SAS_PROVIDER: str = "org.apache.hadoop.fs.azurebfs.sas.FixedSASTokenProvider"


def _nonempty(value: Any) -> str | None:
    """Return ``value`` as a stripped string or ``None`` when blank."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_connection_string(conn_str: str) -> dict[str, str]:
    """Split a portal-style connection string into its k=v pairs.

    The Azure portal's *Access keys* blade emits strings of the form
    ``DefaultEndpointsProtocol=https;AccountName=…;AccountKey=…;
    EndpointSuffix=core.windows.net``. Returns an empty dict when the
    input is malformed rather than raising — the caller's mode
    resolution will then fall through to the next candidate.
    """
    out: dict[str, str] = {}
    for part in conn_str.split(";"):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        value = value.strip()
        if key and value:
            out[key] = value
    return out


def resolve_address_auth_mode(config: dict[str, Any]) -> str:
    """Classify a merged address-config into an ABFS auth mode.

    Returns one of:

    - ``"connection_string"`` — full portal connection string; overrides
      every other leg because it already carries an endpoint.
    - ``"account_key"`` — shared account key.
    - ``"sas_token"`` — shared access signature.
    - ``"service_principal"`` — Azure AD SPN (tenant + client id +
      client secret).
    - ``"managed_identity"`` — no credential fields; the ABFS driver
      is configured to use ``MsiTokenProvider``. This is the default
      trust path on the Ovid host.

    Half-populated service-principal secrets (e.g. tenant + client id
    without a secret) fall through to ``managed_identity`` — matches
    the pre-D3.azure_blob address behaviour.
    """
    if _nonempty(config.get("connection_string")):
        return "connection_string"
    if _nonempty(config.get("account_key")):
        return "account_key"
    if _nonempty(config.get("sas_token")):
        return "sas_token"
    if (
        _nonempty(config.get("tenant_id"))
        and _nonempty(config.get("client_id"))
        and _nonempty(config.get("client_secret"))
    ):
        return "service_principal"
    return "managed_identity"


def resolve_connection_auth_mode(secrets: dict[str, Any]) -> str:
    """Classify a connection's secret dict into its probe auth mode.

    Same five-leg fallback as :func:`resolve_address_auth_mode` except
    the final leg is called ``"default_chain"`` because the probe
    (not the ABFS driver) is what runs — it invokes
    :class:`azure.identity.DefaultAzureCredential`, which walks env
    vars → managed identity → az CLI → ….
    """
    if _nonempty(secrets.get("connection_string")):
        return "connection_string"
    if _nonempty(secrets.get("account_key")):
        return "account_key"
    if _nonempty(secrets.get("sas_token")):
        return "sas_token"
    if (
        _nonempty(secrets.get("tenant_id"))
        and _nonempty(secrets.get("client_id"))
        and _nonempty(secrets.get("client_secret"))
    ):
        return "service_principal"
    return "default_chain"


#: Backwards-compatible alias — old callers spelled the classifier
#: as ``resolve_auth_mode`` before D3.azure_blob split it in two.
resolve_auth_mode = resolve_address_auth_mode


def connection_fields() -> ConnectionFieldSpec:
    """Return the connection-form field spec for an azure_blob connection.

    Mirrors the entry that lives at
    :data:`ovid2.connections.registry._FIELD_SPECS["azure_blob"]`; kept
    byte-identical so the D0 catalogue-sync tests keep passing.
    """
    return ConnectionFieldSpec(
        args=(
            FieldSpec(
                name="account_name",
                required=False,
                help=(
                    "Storage account name (required unless a connection_string secret is provided)."
                ),
            ),
            FieldSpec(
                name="container_default",
                required=False,
                help=("Default container (address may override). Probed by test() when set."),
            ),
            FieldSpec(
                name="endpoint_suffix",
                required=False,
                help=(
                    "e.g. core.windows.net (Azure Public), "
                    "core.usgovcloudapi.net, core.chinacloudapi.cn."
                ),
            ),
        ),
        secrets=(
            FieldSpec(
                name="connection_string",
                required=False,
                help=(
                    "Full connection string from the Azure portal "
                    "(Access keys). Overrides every other auth mode."
                ),
            ),
            FieldSpec(
                name="account_key",
                required=False,
                help=("Storage account key. Mutually exclusive with sas_token / SPN."),
            ),
            FieldSpec(
                name="sas_token",
                required=False,
                help="SAS token (with or without leading '?').",
            ),
            FieldSpec(
                name="tenant_id",
                required=False,
                help="Azure AD tenant id for service-principal auth.",
            ),
            FieldSpec(
                name="client_id",
                required=False,
                help=("Azure AD app registration client id (with tenant_id + client_secret)."),
            ),
            FieldSpec(
                name="client_secret",
                required=False,
                help="Azure AD app registration client secret.",
            ),
        ),
        note=(
            "Leave every secret blank to authenticate with this host's "
            "managed identity (equivalent to `azcopy login --identity`). "
            "Any populated secret takes precedence in the documented "
            "order: connection_string > account_key > sas_token > "
            "service_principal > managed identity."
        ),
    )


def location_fields() -> tuple[LocationField, ...]:
    """Return the location-form field spec for an azure_blob address.

    Mirrors the entry that lives at
    :data:`ovid2.connections.registry._LOCATION_SPECS["azure_blob"]`.
    """
    return (
        LocationField(
            name="container",
            required=False,
            widget="str",
            help=("Container name. Falls back to the connection's container_default when blank."),
        ),
        LocationField(
            name="path",
            required=False,
            widget="str",
            help="Blob-name prefix within the container.",
        ),
        LocationField(
            name="format",
            required=False,
            widget="str",
            default="parquet",
            help="File format (parquet, csv, json, …).",
        ),
        LocationField(
            name="single_file",
            required=False,
            widget="bool",
            default=False,
            help=(
                "CSV only: when true, stitch the Spark part-files into "
                "one ``{table}.csv`` blob via a server-side ADLS block "
                "commit (``Put Block From URL``, no bytes through the "
                "driver). Default is false — downstream consumers "
                "(Snowflake, Databricks Autoloader, Synapse external "
                "tables) accept the raw part directory natively."
            ),
        ),
    )


def build_address_config(
    args: dict[str, Any],
    secrets: dict[str, Any],
    location: dict[str, Any],
) -> dict[str, Any]:
    """Merge ``(args, secrets, location)`` into an address config dict.

    Reproduces byte-identically the shape
    :meth:`AzureBlobConnection.as_address_config` used to emit before
    D3.azure_blob, so the legacy :class:`AzureBlobAddress` shim
    receives exactly the same payload it did pre-refactor.

    Non-secret connection args (``account_name``, ``endpoint_suffix``)
    land at the top level. Every populated secret is forwarded
    verbatim; the address then classifies them via
    :func:`resolve_address_auth_mode`. ``container`` falls back to
    the connection's ``container_default`` when the location leaves
    it blank.
    """
    merged: dict[str, Any] = {}
    for k in ("account_name", "endpoint_suffix"):
        v = args.get(k)
        if v is not None:
            merged[k] = v
    for k in (
        "connection_string",
        "account_key",
        "sas_token",
        "tenant_id",
        "client_id",
        "client_secret",
    ):
        v = secrets.get(k)
        if v is not None:
            merged[k] = v
    container = location.get("container") or args.get("container_default")
    if container:
        merged["container"] = container
    if location.get("path") is not None:
        merged["prefix"] = str(location["path"])
    if location.get("format") is not None:
        merged["format"] = str(location["format"])
    if location.get("single_file") is not None:
        merged["single_file"] = bool(location["single_file"])
    return merged


__all__ = [
    "_CLIENT_CREDS_PROVIDER",
    "_FIXED_SAS_PROVIDER",
    "_MSI_PROVIDER",
    "_parse_connection_string",
    "build_address_config",
    "connection_fields",
    "location_fields",
    "resolve_address_auth_mode",
    "resolve_auth_mode",
    "resolve_connection_auth_mode",
]
