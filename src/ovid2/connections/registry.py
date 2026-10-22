"""Connector registry and JSON-schema-shaped field spec catalogue.

Two responsibilities:

1. Map short type names (``disc``, ``snowflake``, …) to dotted class
   paths; the loader imports lazily so a missing driver only errors
   out when the operator actually asks for that type.
2. Publish a **field spec catalogue** describing the shape of each
   connector's form: which keys go into ``args`` (non-secret) and
   which into ``secrets`` (mode-0o600). The web UI and MCP tools
   consume this to render the dynamic form and validate submissions.

The catalogue deliberately duplicates the shape information that the
concrete class also knows (e.g. that a Snowflake connection wants an
``account`` argument). That duplication is the price of keeping the
loader-side registry independent of the class-side imports: the
control-panel process must not import Spark just to enumerate the
Snowflake fields.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ovid2.connections.base import BaseConnection

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: Type name → dotted class path.
CONNECTION_TYPE_MAP: dict[str, str] = {
    "disc": "ovid2.drivers.disc.DiscConnection",
    "snowflake": "ovid2.drivers.snowflake.SnowflakeConnection",
    "s3": "ovid2.drivers.s3.S3Connection",
    "azure_blob": "ovid2.drivers.azure_blob.AzureBlobConnection",
    "access": "ovid2.drivers.access.AccessConnection",
    "gcs": "ovid2.connections.gcs.GcsConnection",
    "databricks": "ovid2.connections.databricks.DatabricksConnection",
    "postgres": "ovid2.connections.postgres.PostgresConnection",
    "mssql": "ovid2.connections.mssql.MssqlConnection",
}


def load_connection_class(type_name: str) -> type[BaseConnection]:
    """Import and return the class registered under ``type_name``.

    Args:
        type_name: One of the keys in :data:`CONNECTION_TYPE_MAP`.

    Returns:
        The concrete :class:`BaseConnection` subclass.

    Raises:
        KeyError: When ``type_name`` is not registered.
    """
    if type_name not in CONNECTION_TYPE_MAP:
        raise KeyError(
            f"Unknown connection type '{type_name}'. "
            f"Registered: {sorted(CONNECTION_TYPE_MAP.keys())}."
        )
    module_path, class_name = CONNECTION_TYPE_MAP[type_name].rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)  # type: ignore[no-any-return]


def list_connection_types() -> list[str]:
    """Return the sorted list of registered connector type names."""
    return sorted(CONNECTION_TYPE_MAP.keys())


# ---------------------------------------------------------------------------
# Field spec catalogue
# ---------------------------------------------------------------------------

#: Per-type description of the form fields the UI should render.
#:
#: Each entry is a dict with keys:
#:
#: - ``args`` — list of non-secret field descriptors (dicts with
#:   ``name``, ``required``, ``default``, ``help``, optional ``multiline``).
#: - ``secrets`` — same shape but stored via :mod:`ovid2.config.secrets`.
#: - ``note`` (optional, string) — connector-level guidance rendered
#:   above the form in the UI (e.g. "leave secrets blank to use the
#:   host's managed identity").
_FIELD_SPECS: dict[str, dict[str, Any]] = {
    "disc": {
        "args": [
            {
                "name": "host_path",
                "required": True,
                "widget": "path_picker",
                "help": "Absolute path on the host. Use the Browse button to "
                "pick a directory. This is what gets bind-mounted into "
                "the ETL container.",
            },
            {
                "name": "container_path",
                "required": False,
                "help": "Path inside the container. Defaults to host_path — "
                "the pipeline and the picker share the same string, "
                "which matches operator muscle memory. Set this only "
                "when the container should see a different path.",
            },
            {
                "name": "mount_mode",
                "required": False,
                "default": "rw",
                "help": "Bind-mount mode applied to the auto-generated mount. "
                "Use 'rw' for outputs and 'ro' for read-only feeds.",
            },
        ],
        "secrets": [],
        "note": (
            "Disc connections auto-generate a Docker bind mount so the "
            "pipeline can read/write outside the standard mount set "
            "(/data, /app, /ovid_credentials, /usr/include/rsa). Legacy "
            "root_path files still load and are surfaced as host_path."
        ),
    },
    "snowflake": {
        "args": [
            {"name": "account", "required": True, "help": "Snowflake account identifier."},
            {"name": "user", "required": True, "help": "Login user."},
            {"name": "warehouse", "required": False, "help": "Default warehouse (optional)."},
            {"name": "role", "required": False, "help": "Default role (optional)."},
            {
                "name": "authenticator",
                "required": False,
                "help": "e.g. externalbrowser / SNOWFLAKE.",
            },
            {
                "name": "credentials_dir",
                "required": False,
                "default": "/usr/include/rsa",
                "help": "Directory containing the RSA key + passphrase files.",
            },
            {
                "name": "private_key_file",
                "required": False,
                "default": "rsa_key.p8",
                "help": "Filename of the .p8 private key inside credentials_dir.",
            },
            {
                "name": "private_key_passphrase_file",
                "required": False,
                "default": "credentials",
                "help": "Filename of the passphrase file inside credentials_dir. Leave blank for an unencrypted key.",
            },
        ],
        "secrets": [
            {
                "name": "password",
                "required": False,
                "help": "Password (only for password-auth accounts; leave blank when using key-pair auth).",
            },
        ],
    },
    "s3": {
        "args": [
            {"name": "region", "required": True, "help": "AWS region (e.g. us-east-1)."},
            {
                "name": "endpoint_url",
                "required": False,
                "help": "Override for S3-compatible services.",
            },
            {
                "name": "bucket_default",
                "required": False,
                "help": "Default bucket (address may override).",
            },
            {"name": "signature_version", "required": False, "help": "e.g. s3v4."},
        ],
        "secrets": [
            {"name": "aws_access_key_id", "required": True, "help": "AWS access key id."},
            {"name": "aws_secret_access_key", "required": True, "help": "AWS secret access key."},
            {"name": "aws_session_token", "required": False, "help": "Optional STS session token."},
        ],
    },
    "azure_blob": {
        "args": [
            {
                "name": "account_name",
                "required": False,
                "help": "Storage account name (required unless a connection_string secret is provided).",
            },
            {
                "name": "container_default",
                "required": False,
                "help": "Default container (address may override). Probed by test() when set.",
            },
            {
                "name": "endpoint_suffix",
                "required": False,
                "help": "e.g. core.windows.net (Azure Public), core.usgovcloudapi.net, core.chinacloudapi.cn.",
            },
        ],
        "secrets": [
            {
                "name": "connection_string",
                "required": False,
                "help": "Full connection string from the Azure portal (Access keys). Overrides every other auth mode.",
            },
            {
                "name": "account_key",
                "required": False,
                "help": "Storage account key. Mutually exclusive with sas_token / SPN.",
            },
            {
                "name": "sas_token",
                "required": False,
                "help": "SAS token (with or without leading '?').",
            },
            {
                "name": "tenant_id",
                "required": False,
                "help": "Azure AD tenant id for service-principal auth.",
            },
            {
                "name": "client_id",
                "required": False,
                "help": "Azure AD app registration client id (with tenant_id + client_secret).",
            },
            {
                "name": "client_secret",
                "required": False,
                "help": "Azure AD app registration client secret.",
            },
        ],
        "note": (
            "Leave every secret blank to authenticate with this host's "
            "managed identity (equivalent to `azcopy login --identity`). "
            "Any populated secret takes precedence in the documented "
            "order: connection_string > account_key > sas_token > "
            "service_principal > managed identity."
        ),
    },
    "access": {
        "args": [
            {
                "name": "host_path",
                "required": True,
                "widget": "path_picker",
                "help": "Absolute path on the host to the directory holding "
                "the Access database file(s). Bind-mounted (read-only "
                "by default) into the ETL container.",
            },
            {
                "name": "container_path",
                "required": False,
                "help": "Path inside the container. Defaults to host_path.",
            },
            {
                "name": "mount_mode",
                "required": False,
                "default": "ro",
                "help": "Bind-mount mode. Defaults to 'ro' — an Access source is read-only.",
            },
        ],
        "secrets": [],
        "note": (
            "Access connections read Microsoft Access (.accdb / .mdb) "
            "database files via the UCanAccess JDBC driver baked into the "
            "ETL image. Each user table inside a file is surfaced as a "
            "separate table; system tables are hidden. The connection "
            "directory is auto-mounted read-only."
        ),
    },
    "gcs": {
        "args": [
            {"name": "project_id", "required": True, "help": "GCP project id."},
            {
                "name": "bucket_default",
                "required": False,
                "help": "Default bucket (address may override).",
            },
        ],
        "secrets": [
            {
                "name": "service_account_json",
                "required": True,
                "help": "Full service-account JSON, one line.",
                "multiline": True,
            },
        ],
    },
    "databricks": {
        "args": [
            {"name": "host", "required": True, "help": "Workspace URL (https://…)."},
            {"name": "http_path", "required": True, "help": "SQL Warehouse HTTP path."},
            {"name": "catalog", "required": False, "help": "Default Unity Catalog (optional)."},
        ],
        "secrets": [
            {"name": "token", "required": True, "help": "Personal access token."},
        ],
    },
    "postgres": {
        "args": [
            {"name": "host", "required": True, "help": "Server host."},
            {
                "name": "port",
                "required": False,
                "default": 5432,
                "help": "Server port (default 5432).",
            },
            {"name": "database", "required": True, "help": "Default database."},
            {"name": "user", "required": True, "help": "Login user."},
            {"name": "sslmode", "required": False, "help": "e.g. require / prefer / disable."},
        ],
        "secrets": [
            {"name": "password", "required": True, "help": "Password for the login user."},
        ],
    },
    "mssql": {
        "args": [
            {"name": "host", "required": True, "help": "Server host."},
            {
                "name": "port",
                "required": False,
                "default": 1433,
                "help": "Server port (default 1433).",
            },
            {"name": "database", "required": True, "help": "Default database."},
            {"name": "user", "required": True, "help": "Login user."},
            {"name": "encrypt", "required": False, "help": "true/false."},
            {"name": "trust_server_certificate", "required": False, "help": "true/false."},
        ],
        "secrets": [
            {"name": "password", "required": True, "help": "Password for the login user."},
        ],
    },
}


def connection_field_spec(type_name: str) -> dict[str, Any]:
    """Return the ``{args, secrets, note}`` field spec for a connector type.

    Args:
        type_name: One of the keys in :data:`CONNECTION_TYPE_MAP`.

    Returns:
        A dict with three keys:

        - ``args`` — list of non-secret field descriptors.
        - ``secrets`` — list of secret field descriptors (values live in
          ``secrets.d/*.json`` on-disk, mode ``0o600``).
        - ``note`` — optional connector-level guidance string, ``None``
          when the connector has no top-level note.

    Raises:
        KeyError: When ``type_name`` is not registered.
    """
    if type_name not in _FIELD_SPECS:
        raise KeyError(
            f"No field spec for connection type '{type_name}'. "
            f"Registered: {sorted(_FIELD_SPECS.keys())}."
        )
    # Shallow copy so callers cannot mutate the module-level catalogue.
    spec = _FIELD_SPECS[type_name]
    args_field = spec.get("args", [])
    secrets_field = spec.get("secrets", [])
    note_field = spec.get("note")
    return {
        "args": [dict(f) for f in args_field] if isinstance(args_field, list) else [],
        "secrets": [dict(f) for f in secrets_field] if isinstance(secrets_field, list) else [],
        "note": str(note_field) if isinstance(note_field, str) else None,
    }


# ---------------------------------------------------------------------------
# Location field spec catalogue
# ---------------------------------------------------------------------------

#: Per-type description of the *location* fields — the small payload a
#: W7 address carries on top of a connection ("the point in the
#: dimension"). Mirrors the shape of :data:`_FIELD_SPECS` but with a
#: single ``fields`` list per type, since locations never carry
#: secrets: credentials always live on the connection.
#:
#: Each field descriptor is a dict with keys:
#:
#: - ``name`` — payload key stored under ``location`` in the JSON.
#: - ``required`` — whether the field must be non-empty.
#: - ``type`` (optional) — ``str`` (default), ``bool``, or ``int``.
#:   Consumed by the HTML form renderer to pick the input widget.
#: - ``default`` (optional) — placeholder / initial value.
#: - ``help`` — one-line description shown next to the input.
_LOCATION_SPECS: dict[str, list[dict[str, Any]]] = {
    "disc": [
        {
            "name": "path",
            "required": False,
            "type": "str",
            "help": "Sub-path relative to the connection's root_path. "
            "Leave blank to use the root itself.",
        },
        {
            "name": "format",
            "required": False,
            "type": "str",
            "default": "parquet",
            "help": "File format for reads/writes (parquet, csv, json, …).",
        },
        {
            "name": "lowercase_columns",
            "required": False,
            "type": "bool",
            "help": "Lowercase every column name on read.",
        },
        {
            "name": "single_file",
            "required": False,
            "type": "bool",
            "default": True,
            "help": "CSV only: merge Spark part-files into a single {table}.csv "
            "on write. Default true for disc.",
        },
    ],
    "snowflake": [
        {
            "name": "database",
            "required": True,
            "type": "str",
            "help": "Snowflake database this address reads/writes.",
        },
        {
            "name": "schema",
            "required": False,
            "type": "str",
            "help": "Default schema within the database (optional).",
        },
    ],
    "s3": [
        {
            "name": "bucket",
            "required": False,
            "type": "str",
            "help": "Bucket name. Falls back to the connection's bucket_default when blank.",
        },
        {
            "name": "path",
            "required": False,
            "type": "str",
            "help": "Object-key prefix within the bucket.",
        },
        {
            "name": "format",
            "required": False,
            "type": "str",
            "default": "parquet",
            "help": "File format (parquet, csv, json, …).",
        },
        {
            "name": "single_file",
            "required": False,
            "type": "bool",
            "default": False,
            "help": "CSV only: stitch part-files into one object via a "
            "server-side S3 multipart copy. Default false.",
        },
    ],
    "azure_blob": [
        {
            "name": "container",
            "required": False,
            "type": "str",
            "help": "Container name. Falls back to the connection's container_default when blank.",
        },
        {
            "name": "path",
            "required": False,
            "type": "str",
            "help": "Blob-name prefix within the container.",
        },
        {
            "name": "format",
            "required": False,
            "type": "str",
            "default": "parquet",
            "help": "File format (parquet, csv, json, …).",
        },
        {
            "name": "single_file",
            "required": False,
            "type": "bool",
            "default": False,
            "help": "CSV only: stitch part-files into one blob via a "
            "server-side ADLS block commit. Default false.",
        },
    ],
    "access": [
        {
            "name": "path",
            "required": True,
            "type": "str",
            "help": "Path to the .accdb / .mdb file, relative to the connection's host_path.",
        },
        {
            "name": "tables",
            "required": False,
            "type": "str",
            "help": "Optional comma-separated allow-list of table names. "
            "Blank reads every user table (one output each).",
        },
        {
            "name": "exclude_tables",
            "required": False,
            "type": "str",
            "help": "Optional comma-separated deny-list of table names. Applied after 'tables'.",
        },
        {
            "name": "lowercase_columns",
            "required": False,
            "type": "bool",
            "help": "Lowercase every column name on read.",
        },
    ],
    "gcs": [
        {
            "name": "bucket",
            "required": False,
            "type": "str",
            "help": "Bucket name. Falls back to the connection's bucket_default when blank.",
        },
        {
            "name": "path",
            "required": False,
            "type": "str",
            "help": "Object-name prefix within the bucket.",
        },
        {
            "name": "format",
            "required": False,
            "type": "str",
            "default": "parquet",
            "help": "File format (parquet, csv, json, …).",
        },
        {
            "name": "single_file",
            "required": False,
            "type": "bool",
            "default": False,
            "help": "CSV only: request a single-file write via Snowflake "
            "``copy_into_location(single=True)`` (Snowpark path). "
            "Ignored on the pyspark path today. Default false.",
        },
    ],
    "databricks": [
        {
            "name": "catalog",
            "required": False,
            "type": "str",
            "help": "Unity Catalog name. Falls back to the connection's catalog when blank.",
        },
        {
            "name": "schema",
            "required": False,
            "type": "str",
            "help": "Default schema within the catalog (optional).",
        },
    ],
    "postgres": [
        {
            "name": "database",
            "required": False,
            "type": "str",
            "help": "Database name. Falls back to the connection's default database when blank.",
        },
        {
            "name": "schema",
            "required": False,
            "type": "str",
            "help": "Default schema within the database (optional).",
        },
    ],
    "mssql": [
        {
            "name": "database",
            "required": False,
            "type": "str",
            "help": "Database name. Falls back to the connection's default database when blank.",
        },
        {
            "name": "schema",
            "required": False,
            "type": "str",
            "help": "Default schema within the database (optional).",
        },
    ],
}


def location_field_spec(type_name: str) -> list[dict[str, Any]]:
    """Return the location-field descriptors for a connector type.

    The list describes what the *address* form should ask the operator
    for when the underlying connection is of ``type_name``. Locations
    never carry credentials — every secret lives on the connection.

    Args:
        type_name: One of the keys in :data:`CONNECTION_TYPE_MAP`.

    Returns:
        A shallow-copied list of field descriptor dicts. Never
        ``None`` — connectors with no per-address knobs return an
        empty list.

    Raises:
        KeyError: When ``type_name`` is not registered.
    """
    if type_name not in _LOCATION_SPECS:
        raise KeyError(
            f"No location spec for connection type '{type_name}'. "
            f"Registered: {sorted(_LOCATION_SPECS.keys())}."
        )
    return [dict(f) for f in _LOCATION_SPECS[type_name]]
