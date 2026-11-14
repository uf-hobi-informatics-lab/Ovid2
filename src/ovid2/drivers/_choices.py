"""Closed value lists shared by the connection catalogue and driver metadata.

UI rule U2 (``AGENTS.md``): a form field whose value must come from a
known list renders as a ``<select>`` over that list, never free text.
The web catalogue (:mod:`ovid2.connections.registry`) and each driver's
``metadata.py`` both declare ``widget="choices"`` over the tuples below,
so the two catalogues cannot drift (pinned by
``tests/contract/test_driver_field_specs_match_catalogue.py``).

Import-safe: no SDK imports (``test_driver_metadata_import_safe.py``).
"""

from __future__ import annotations

#: File formats every folder-store driver (disc / s3 / azure_blob / gcs)
#: reads and writes on both the Spark and DuckDB data planes.
FILE_FORMATS: tuple[str, ...] = ("parquet", "csv", "json")

#: Bind-mount modes for disc / access connections.
MOUNT_MODES: tuple[str, ...] = ("rw", "ro")

#: Snowflake ``authenticator`` values accepted by the connector.
SNOWFLAKE_AUTHENTICATORS: tuple[str, ...] = (
    "snowflake",
    "externalbrowser",
    "snowflake_jwt",
    "oauth",
    "username_password_mfa",
)

#: S3 request-signing algorithms (``s3v4`` for AWS; ``s3`` for legacy
#: S3-compatible stores).
S3_SIGNATURE_VERSIONS: tuple[str, ...] = ("s3v4", "s3")

#: libpq ``sslmode`` values.
POSTGRES_SSLMODES: tuple[str, ...] = (
    "disable",
    "allow",
    "prefer",
    "require",
    "verify-ca",
    "verify-full",
)

#: ODBC Driver 18 ``Encrypt=`` values.
MSSQL_ENCRYPT: tuple[str, ...] = ("yes", "no", "strict")

#: ODBC ``TrustServerCertificate=`` values.
MSSQL_TRUST_CERT: tuple[str, ...] = ("no", "yes")
