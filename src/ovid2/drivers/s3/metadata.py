"""Import-safe metadata for the ``s3`` driver.

Everything the web tier and MCP need to describe the S3 peripheral —
form fields, location fields, address-config resolution. Deliberately
free of pyspark and boto3 imports so :mod:`ovid2.drivers.s3` can be
enumerated by the web control panel without loading either.

Contains:

- :data:`_VALID_FORMATS` — recognised on-disk formats.
- :data:`_SIMPLE_CREDS_PROVIDER` / :data:`_TEMPORARY_CREDS_PROVIDER` /
  :data:`_DEFAULT_CREDS_PROVIDER` — Hadoop ``s3a`` credentials-provider
  class names (used by :mod:`ovid2.drivers.s3.spark`; re-exported so
  the pre-D3.s3 test suite's imports from
  ``ovid2.core.addresses.s3`` translate cleanly).
- :func:`resolve_auth_mode` — pure classifier over the credential
  triple. Used by both the driver (for its :attr:`auth_mode` display
  property) and the legacy :class:`S3Address` shim.
- :func:`connection_fields` / :func:`location_fields` — the field-spec
  catalogue the D2-era :data:`_FIELD_SPECS` / :data:`_LOCATION_SPECS`
  used to publish, returned as typed
  :class:`~ovid2.drivers.base.ConnectionFieldSpec` and
  :class:`~ovid2.drivers.base.LocationField` tuples.
- :func:`build_address_config` — the merger that
  :meth:`~ovid2.connections.base.BaseConnection.as_address_config`
  previously implemented inline in :mod:`ovid2.connections.s3`.
  Kept as a module-level function so the driver's data-plane and the
  legacy shim share one shape.
"""

from __future__ import annotations

from typing import Any

from ovid2.drivers.base import ConnectionFieldSpec, FieldSpec, LocationField

#: Recognised on-disk formats. Extension is tracked but not enforced —
#: unknown formats surface as a ``ValueError`` at read/write time so
#: mis-authored addresses fail loudly (matches the pre-refactor
#: behaviour).
_VALID_FORMATS: frozenset[str] = frozenset({"csv", "parquet", "json"})


#: Class name of the Hadoop ``SimpleAWSCredentialsProvider`` — static
#: access-key / secret-key pair (no session token).
_SIMPLE_CREDS_PROVIDER: str = "org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider"

#: Class name of the Hadoop ``TemporaryAWSCredentialsProvider`` — same
#: as :data:`_SIMPLE_CREDS_PROVIDER` but for STS session credentials.
_TEMPORARY_CREDS_PROVIDER: str = "org.apache.hadoop.fs.s3a.TemporaryAWSCredentialsProvider"

#: Class name of the AWS SDK default provider chain (EC2 metadata,
#: ECS/EKS task credentials, ``AWS_*`` env, etc.). Used when the
#: connection has no static credential fields.
_DEFAULT_CREDS_PROVIDER: str = "com.amazonaws.auth.DefaultAWSCredentialsProviderChain"


def _nonempty(value: Any) -> str | None:
    """Return ``value`` as a stripped string or ``None`` when blank."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def resolve_auth_mode(config: dict[str, Any]) -> str:
    """Classify the credential triple into an auth mode.

    Returns one of:

    - ``"session"`` — access-key pair *and* session token are present
      (STS credentials).
    - ``"static"`` — access-key pair is present without a session token.
    - ``"default_chain"`` — neither leg is fully populated; the driver
      falls back to the AWS SDK default provider chain (EC2 metadata,
      IRSA, ``AWS_*`` env, etc.).

    Half-populated static credentials (only one of access-key /
    secret-key) fall through to ``default_chain`` deliberately — the
    Hadoop driver would emit an obscure 400 signing error otherwise.
    """
    aki = _nonempty(config.get("aws_access_key_id"))
    ask = _nonempty(config.get("aws_secret_access_key"))
    ast_ = _nonempty(config.get("aws_session_token"))
    if aki and ask and ast_:
        return "session"
    if aki and ask:
        return "static"
    return "default_chain"


def connection_fields() -> ConnectionFieldSpec:
    """Return the connection-form field spec for an S3 connection.

    Mirrors the entry that lived at
    :data:`ovid2.connections.registry._FIELD_SPECS["s3"]` before D3.s3;
    kept byte-identical so the D0 catalogue-sync tests keep passing.
    """
    return ConnectionFieldSpec(
        args=(
            FieldSpec(
                name="region",
                required=True,
                help="AWS region (e.g. us-east-1).",
            ),
            FieldSpec(
                name="endpoint_url",
                required=False,
                help="Override for S3-compatible services.",
            ),
            FieldSpec(
                name="bucket_default",
                required=False,
                help="Default bucket (address may override).",
            ),
            FieldSpec(
                name="signature_version",
                required=False,
                help="e.g. s3v4.",
            ),
        ),
        secrets=(
            FieldSpec(
                name="aws_access_key_id",
                required=True,
                help="AWS access key id.",
            ),
            FieldSpec(
                name="aws_secret_access_key",
                required=True,
                help="AWS secret access key.",
            ),
            FieldSpec(
                name="aws_session_token",
                required=False,
                help="Optional STS session token.",
            ),
        ),
    )


def location_fields() -> tuple[LocationField, ...]:
    """Return the location-form field spec for an S3 address.

    Mirrors the entry that lived at
    :data:`ovid2.connections.registry._LOCATION_SPECS["s3"]` before
    D3.s3.
    """
    return (
        LocationField(
            name="bucket",
            required=False,
            widget="str",
            help=("Bucket name. Falls back to the connection's bucket_default when blank."),
        ),
        LocationField(
            name="path",
            required=False,
            widget="str",
            help="Object-key prefix within the bucket.",
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
                "one ``{table}.csv`` object via a server-side S3 "
                "multipart copy (no bytes through the driver). Default "
                "is false — downstream consumers (Snowflake COPY INTO, "
                "Athena, Databricks Autoloader) accept the raw part "
                "prefix natively."
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
    :meth:`~ovid2.connections.s3.S3Connection.as_address_config`
    used to emit before D3.s3, so the legacy :class:`S3Address` shim
    receives exactly the same payload it did pre-refactor.

    Non-secret connection args (``region``, ``endpoint_url``,
    ``signature_version``) land at the top level. Secrets
    (``aws_access_key_id``, ``aws_secret_access_key``,
    ``aws_session_token``) are merged in verbatim; the address then
    classifies them via :func:`resolve_auth_mode`. ``bucket`` falls
    back to the connection's ``bucket_default`` when the location
    leaves it blank.
    """
    merged: dict[str, Any] = {}
    for k in ("region", "endpoint_url", "signature_version"):
        v = args.get(k)
        if v is not None:
            merged[k] = v
    for k in ("aws_access_key_id", "aws_secret_access_key", "aws_session_token"):
        v = secrets.get(k)
        if v is not None:
            merged[k] = v
    bucket = location.get("bucket") or args.get("bucket_default")
    if bucket:
        merged["bucket"] = bucket
    if location.get("path") is not None:
        merged["prefix"] = str(location["path"])
    if location.get("format") is not None:
        merged["format"] = str(location["format"])
    if location.get("single_file") is not None:
        merged["single_file"] = bool(location["single_file"])
    return merged


__all__ = [
    "build_address_config",
    "connection_fields",
    "location_fields",
    "resolve_auth_mode",
]
