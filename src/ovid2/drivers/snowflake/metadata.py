"""Import-safe metadata for the ``snowflake`` driver.

Everything the web tier and MCP need to describe the Snowflake
peripheral — form fields, location fields, credential-file paths.
Deliberately free of pyspark and ``snowflake-*`` / ``cryptography``
imports so :mod:`ovid2.drivers.snowflake` can be enumerated by the
web control panel without pulling in either.

Contains:

- Legacy Ovid defaults (:data:`DEFAULT_CREDENTIALS_DIR`,
  :data:`DEFAULT_PRIVATE_KEY_FILE`, :data:`DEFAULT_PASSPHRASE_FILE`).
- :func:`connection_fields` / :func:`location_fields` — typed
  :class:`~ovid2.drivers.base.ConnectionFieldSpec` /
  :class:`~ovid2.drivers.base.LocationField` catalogues mirroring
  the entries in :mod:`ovid2.connections.registry`.
- :func:`build_address_config` — merger that
  :meth:`SnowflakeConnection.as_address_config` used to implement
  inline. Keeps the ``file-location credential`` model: the merger
  reads the PEM + passphrase files on disk and emits DER-encoded
  ``private_key`` bytes plus the resolved ``account`` / ``user`` /
  ``warehouse`` / ``role`` / ``authenticator`` so the pipeline-side
  ``SnowflakeAddress`` receives a ready-to-use ``src_args`` dict.
"""

from __future__ import annotations

import logging
from typing import Any

from ovid2.drivers.base import ConnectionFieldSpec, FieldSpec, LocationField

logger = logging.getLogger(__name__)

#: Legacy Ovid default for ``CREDENTIALS_PATH``.
DEFAULT_CREDENTIALS_DIR: str = "/usr/include/rsa"

#: Legacy Ovid default for ``SF_PRIVATE_KEY_FILE_NAME``.
DEFAULT_PRIVATE_KEY_FILE: str = "rsa_key.p8"

#: Legacy Ovid default for ``SF_PRIVATE_KEY_PASSPHRASE_FILE_NAME``.
DEFAULT_PASSPHRASE_FILE: str = "credentials"


def connection_fields() -> ConnectionFieldSpec:
    """Return the connection-form field spec for a Snowflake connection.

    Mirrors the entry that lives at
    :data:`ovid2.connections.registry._FIELD_SPECS["snowflake"]` byte-
    for-byte so the D0 catalogue-sync tests keep passing.
    """
    return ConnectionFieldSpec(
        args=(
            FieldSpec(
                name="account",
                required=True,
                help="Snowflake account identifier.",
            ),
            FieldSpec(
                name="user",
                required=True,
                help="Login user.",
            ),
            FieldSpec(
                name="warehouse",
                required=False,
                help="Default warehouse (optional).",
            ),
            FieldSpec(
                name="role",
                required=False,
                help="Default role (optional).",
            ),
            FieldSpec(
                name="authenticator",
                required=False,
                help="e.g. externalbrowser / SNOWFLAKE.",
            ),
            FieldSpec(
                name="credentials_dir",
                required=False,
                default=DEFAULT_CREDENTIALS_DIR,
                help=(
                    "Directory containing the RSA key + passphrase files."
                ),
            ),
            FieldSpec(
                name="private_key_file",
                required=False,
                default=DEFAULT_PRIVATE_KEY_FILE,
                help=(
                    "Filename of the .p8 private key inside "
                    "credentials_dir."
                ),
            ),
            FieldSpec(
                name="private_key_passphrase_file",
                required=False,
                default=DEFAULT_PASSPHRASE_FILE,
                help=(
                    "Filename of the passphrase file inside "
                    "credentials_dir. Leave blank for an unencrypted key."
                ),
            ),
        ),
        secrets=(
            FieldSpec(
                name="password",
                required=False,
                help=(
                    "Password (only for password-auth accounts; leave "
                    "blank when using key-pair auth)."
                ),
            ),
        ),
    )


def location_fields() -> tuple[LocationField, ...]:
    """Return the location-form field spec for a Snowflake address.

    Mirrors the entry that lives at
    :data:`ovid2.connections.registry._LOCATION_SPECS["snowflake"]`.
    """
    return (
        LocationField(
            name="database",
            required=True,
            widget="str",
            help="Snowflake database this address reads/writes.",
        ),
        LocationField(
            name="schema",
            required=False,
            widget="str",
            help="Default schema within the database (optional).",
        ),
    )


def build_address_config(
    args: dict[str, Any],
    secrets: dict[str, Any],
    location: dict[str, Any],
) -> dict[str, Any]:
    """Merge ``(args, secrets, location)`` into an address config dict.

    Reproduces byte-identically the shape
    :meth:`~ovid2.drivers.snowflake.legacy.SnowflakeConnection.as_address_config`
    used to emit before D3.snowflake, so any downstream consumer
    (Spark connector, DDL executor) receives the same payload it did
    pre-refactor.

    Behaviour:

    - Non-secret connection args (``account``, ``user``, ``warehouse``,
      ``role``, ``authenticator``) land at the top level.
    - ``password`` (when set) short-circuits key-pair auth: the merger
      forwards it verbatim and never reads the PEM.
    - Key-pair auth reads
      ``{credentials_dir}/{private_key_file}`` + the passphrase file
      via :func:`~ovid2.drivers.snowflake.probe._read_private_key_pem`
      and :func:`~ovid2.drivers.snowflake.probe._read_passphrase`, and
      emits DER-encoded ``private_key`` bytes. File-read or decode
      errors are logged and swallowed — the pipeline surfaces a
      clearer error when it actually tries to write.
    - ``database`` / ``schema`` come from ``location`` and land at the
      top level. A generic ``location.path`` (``"database"`` or
      ``"database/schema"``) is also accepted — it is split into
      ``database`` / ``schema`` so ``path``-form addresses emitted by
      the web UI resolve correctly instead of falling through to the
      pipeline's ``DEFAULT_PIPELINE_DB_NAME``. Explicit ``database`` /
      ``schema`` keys win over ``path``.

    Args:
        args: Non-secret connection body.
        secrets: Credential dict.
        location: Per-address extras (``database``, ``schema``, or the
            generic ``path``).

    Returns:
        Merged dict of account credentials + per-address location.
    """
    # Local import so this module stays free of the ``cryptography``
    # dependency at collection time.
    from ovid2.drivers.snowflake.probe import (
        _pem_to_der,
        _read_passphrase,
        _read_private_key_pem,
        _resolve_credentials_dir,
    )

    merged: dict[str, Any] = {}
    for k in ("account", "user", "warehouse", "role", "authenticator"):
        v = args.get(k)
        if v is not None:
            merged[k] = v

    password = secrets.get("password")
    if password is not None:
        merged["password"] = password

    # Only attempt key-pair auth when there is no password.
    if not password:
        credentials_dir = _resolve_credentials_dir(args.get("credentials_dir"))
        key_filename = args.get("private_key_file", DEFAULT_PRIVATE_KEY_FILE)
        passphrase_filename = args.get(
            "private_key_passphrase_file", DEFAULT_PASSPHRASE_FILE
        )
        try:
            pem_bytes = _read_private_key_pem(credentials_dir, key_filename)
        except (FileNotFoundError, PermissionError, IsADirectoryError) as exc:
            logger.warning("Snowflake private key unavailable: %s", exc)
            pem_bytes = None
        if pem_bytes is not None:
            try:
                passphrase = _read_passphrase(
                    credentials_dir, passphrase_filename
                )
                merged["private_key"] = _pem_to_der(pem_bytes, passphrase)
            except (FileNotFoundError, PermissionError, IsADirectoryError) as exc:
                logger.warning("Snowflake passphrase unavailable: %s", exc)
            except (ImportError, ValueError, TypeError) as exc:
                logger.warning(
                    "Snowflake private key could not be decoded: %s", exc,
                )

    # ``path`` is the generic location form emitted by the web UI and
    # other tooling that treat every address uniformly (object-store
    # connectors map it to ``prefix``). For Snowflake it encodes
    # ``database`` or ``database/schema`` (e.g. ``CHAR_DEMO`` or
    # ``CHAR_DEMO/PUBLIC``). Split it here so a ``path``-form address is
    # equivalent to the explicit ``database`` / ``schema`` form and does
    # not silently fall through to the pipeline's ``DEFAULT_PIPELINE_DB_NAME``.
    path = location.get("path")
    if path is not None:
        parts = [p for p in str(path).split("/") if p]
        if parts:
            merged["database"] = parts[0]
        if len(parts) > 1:
            merged["schema"] = parts[1]

    # Explicit ``database`` / ``schema`` keys win over anything derived
    # from ``path``.
    for k in ("database", "schema"):
        v = location.get(k)
        if v is not None:
            merged[k] = v
    return merged


__all__ = [
    "DEFAULT_CREDENTIALS_DIR",
    "DEFAULT_PASSPHRASE_FILE",
    "DEFAULT_PRIVATE_KEY_FILE",
    "build_address_config",
    "connection_fields",
    "location_fields",
]
