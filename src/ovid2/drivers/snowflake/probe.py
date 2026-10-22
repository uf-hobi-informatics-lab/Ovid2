"""Snowflake driver probe helpers.

Uses :mod:`snowflake.connector` and :mod:`cryptography` lazily
(imports inside the function bodies) so this module is import-safe:
enumerating drivers does not pull the Snowflake driver or the crypto
library at module load time.

Kept in a dedicated module because the D3 recipe mandates the
four-file split (``metadata`` + ``driver`` + ``probe`` + ``spark``)
so contract tests can enforce import safety per file rather than per
method.

Also owns the credential-file helpers (``_resolve_credentials_dir``,
``_read_private_key_pem``, ``_read_passphrase``, ``_pem_to_der``)
that :mod:`ovid2.drivers.snowflake.metadata.build_address_config`
uses to convert a file-location connection into the DER-bytes
``private_key`` the pipeline consumes. Those helpers stay here (not
in ``metadata.py``) so the ``cryptography`` import stays behind a
function-call boundary — same lazy pattern the S3 / Azure probes use
for ``boto3`` / ``azure-*``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ovid2.drivers.base import ProbeResult, StorageEntry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path + file helpers
# ---------------------------------------------------------------------------


def _resolve_credentials_dir(value: str | None) -> Path:
    """Return the credentials directory, applying the legacy default."""
    # Local import so this helper is import-safe even if metadata is
    # not yet loaded (breaks a potential circular between metadata +
    # probe).
    from ovid2.drivers.snowflake.metadata import DEFAULT_CREDENTIALS_DIR

    return Path(value or DEFAULT_CREDENTIALS_DIR).expanduser()


def _read_private_key_pem(
    credentials_dir: Path,
    filename: str | None,
) -> bytes | None:
    """Return the PEM bytes of the private key file, or ``None`` when unset.

    Args:
        credentials_dir: Directory containing the key file.
        filename: Basename. ``None`` or empty / whitespace → no key
            configured (password auth path); returns ``None``.

    Returns:
        The raw PEM bytes suitable for
        :func:`serialization.load_pem_private_key`.

    Raises:
        FileNotFoundError: When the resolved path does not exist.
        IsADirectoryError: When the resolved path is a directory.
        PermissionError: When the file is not readable.
    """
    name = (filename or "").strip()
    if not name:
        return None
    return (credentials_dir / name).read_bytes()


def _read_passphrase(
    credentials_dir: Path,
    filename: str | None,
) -> str | None:
    """Return the passphrase from the credentials file, or ``None``.

    The first line of the file is returned with leading/trailing
    whitespace stripped, matching legacy Ovid's
    ``pipelines/scheduler.py`` handling of the ``credentials`` file.
    An empty / missing filename means "the key is unencrypted".

    Args:
        credentials_dir: Directory containing the passphrase file.
        filename: Basename (or empty).

    Returns:
        The stripped first line, or ``None`` when the key is
        unencrypted.

    Raises:
        FileNotFoundError: When the resolved path does not exist and
            the operator explicitly requested a passphrase file.
    """
    name = (filename or "").strip()
    if not name:
        return None
    text = (credentials_dir / name).read_text(encoding="utf-8")
    first_line = text.splitlines()[0] if text else ""
    return first_line.strip() or None


def _pem_to_der(pem_bytes: bytes, passphrase: str | None) -> bytes:
    """Convert PEM (possibly encrypted) to unencrypted DER PKCS8 bytes.

    Mirrors the conversion legacy Ovid performs before calling
    ``snowflake.connector.connect`` — the connector wants DER PKCS8
    bytes, not a PEM string.

    Args:
        pem_bytes: Raw PEM contents.
        passphrase: Passphrase for encrypted keys; ``None`` or empty
            for unencrypted keys.

    Returns:
        The key re-encoded as unencrypted DER PKCS8.

    Raises:
        ImportError: When the ``cryptography`` package is not
            installed on the host.
        ValueError: When the PEM cannot be parsed (bad passphrase,
            malformed body, unsupported algorithm, …).
    """
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization

    password_bytes = passphrase.encode("utf-8") if passphrase else None
    private_key = serialization.load_pem_private_key(
        pem_bytes,
        password=password_bytes,
        backend=default_backend(),
    )
    return private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


# ---------------------------------------------------------------------------
# Connect kwargs — shared between the probe and the address's DDL path.
# ---------------------------------------------------------------------------


def build_connect_kwargs(
    args: dict[str, Any], secrets: dict[str, Any],
) -> dict[str, Any]:
    """Return the kwargs dict to pass to ``snowflake.connector.connect``.

    Reads and decodes the key files on every call — the runtime cost
    is negligible and it keeps the caller stateless.

    Authentication mode is chosen by the presence of a ``password``
    secret: password auth when set, key-pair auth otherwise. Key-pair
    auth requires the ``private_key_file`` (default
    :data:`~ovid2.drivers.snowflake.metadata.DEFAULT_PRIVATE_KEY_FILE`)
    to exist under ``credentials_dir`` (default
    :data:`~ovid2.drivers.snowflake.metadata.DEFAULT_CREDENTIALS_DIR`).

    Args:
        args: Non-secret connection body.
        secrets: Credential dict.

    Returns:
        A dict suitable for
        :func:`snowflake.connector.connect` **or**
        :meth:`snowflake.snowpark.Session.SessionBuilder.configs` —
        both accept the same key names.

    Raises:
        FileNotFoundError / PermissionError / IsADirectoryError:
            When the referenced key or passphrase file cannot be read.
        ValueError / TypeError: When the PEM cannot be parsed (bad
            passphrase, malformed body).
    """
    # Local import to keep the metadata-vs-probe dependency edge
    # one-directional (metadata does not import probe).
    from ovid2.drivers.snowflake.metadata import (
        DEFAULT_PASSPHRASE_FILE,
        DEFAULT_PRIVATE_KEY_FILE,
    )

    kwargs: dict[str, Any] = {}
    for k in ("account", "user", "warehouse", "role", "authenticator"):
        v = args.get(k)
        if v:
            kwargs[k] = v

    password = secrets.get("password")
    if password:
        kwargs["password"] = password
        return kwargs

    credentials_dir = _resolve_credentials_dir(args.get("credentials_dir"))
    key_filename = args.get("private_key_file", DEFAULT_PRIVATE_KEY_FILE)
    passphrase_filename = args.get(
        "private_key_passphrase_file", DEFAULT_PASSPHRASE_FILE,
    )
    pem_bytes = _read_private_key_pem(credentials_dir, key_filename)
    if pem_bytes is not None:
        passphrase = _read_passphrase(credentials_dir, passphrase_filename)
        kwargs["private_key"] = _pem_to_der(pem_bytes, passphrase)
    return kwargs


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


def probe_auth_from_config(
    args: dict[str, Any], secrets: dict[str, Any],
) -> ProbeResult:
    """Return an ``auth`` probe result for a Snowflake connection.

    Mirrors the pre-D3.snowflake :meth:`SnowflakeConnection.test`
    behaviour verbatim:

    1. When ``snowflake-connector-python`` is not installed, return
       ``code='skipped'``.
    2. When ``build_connect_kwargs`` fails on
       :exc:`FileNotFoundError` / :exc:`PermissionError` /
       :exc:`IsADirectoryError`, return ``code='auth'``.
    3. When the ``cryptography`` package is missing, return
       ``code='skipped'``.
    4. When PEM parsing fails, return ``code='auth'``.
    5. When the connect step itself raises, return
       ``code='unreachable'``.
    6. When the SELECT 1 round-trip fails, return ``code='auth'``.

    Args:
        args: Non-secret connection body.
        secrets: Credential dict.

    Returns:
        :class:`~ovid2.drivers.base.ProbeResult` with
        ``capability='auth'``.
    """
    try:
        import snowflake.connector as sf  # type: ignore[import-not-found]
    except ImportError:
        return ProbeResult(
            ok=False,
            code="skipped",
            message="snowflake-connector-python is not installed on this host.",
            capability="auth",
        )
    try:
        kwargs = build_connect_kwargs(args, secrets)
    except FileNotFoundError as exc:
        return ProbeResult(
            ok=False,
            code="auth",
            message=f"Credentials file not found: {exc}",
            capability="auth",
        )
    except (PermissionError, IsADirectoryError) as exc:
        return ProbeResult(
            ok=False,
            code="auth",
            message=f"Credentials file is not readable: {exc}",
            capability="auth",
        )
    except ImportError as exc:
        return ProbeResult(
            ok=False,
            code="skipped",
            message=(
                "The 'cryptography' package is required to decode the "
                f"private key: {exc}"
            ),
            capability="auth",
        )
    except (ValueError, TypeError) as exc:
        return ProbeResult(
            ok=False,
            code="auth",
            message=f"Private key could not be parsed: {exc}",
            capability="auth",
        )
    try:
        conn = sf.connect(**kwargs)  # type: ignore[no-untyped-call]
    except Exception as exc:  # noqa: BLE001 — vendor exceptions vary
        return ProbeResult(
            ok=False,
            code="unreachable",
            message=f"Snowflake connect failed: {exc}",
            capability="auth",
        )
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        cursor.close()
    except Exception as exc:  # noqa: BLE001
        conn.close()
        return ProbeResult(
            ok=False,
            code="auth",
            message=f"Snowflake query failed: {exc}",
            capability="auth",
        )
    conn.close()
    return ProbeResult(
        ok=True,
        code="ok",
        message="Snowflake SELECT 1 succeeded.",
        capability="auth",
        detail={"account": str(args.get("account", ""))},
    )


# ---------------------------------------------------------------------------
# Storage browse — database → schema → table hierarchy
# ---------------------------------------------------------------------------

#: Snowflake system databases / schemas hidden from the browser — they
#: never hold pipeline data and only clutter the picker.
_HIDDEN_DATABASES: frozenset[str] = frozenset({"SNOWFLAKE", "SNOWFLAKE_SAMPLE_DATA"})
_HIDDEN_SCHEMAS: frozenset[str] = frozenset({"INFORMATION_SCHEMA"})


def _quote_identifier(name: str) -> str:
    """Return ``name`` as a safely double-quoted Snowflake identifier.

    Snowflake ``SHOW`` commands do not accept bind parameters for the
    object name, so the identifier is interpolated directly. Guard
    against injection by rejecting anything that is not a plain
    identifier (letters, digits, ``_``, ``$``). The values we ever
    pass here come from a prior ``SHOW`` result, never free-form user
    text, but the check is belt-and-suspenders. Embedded double
    quotes are doubled per Snowflake's quoting rules.
    """
    import re

    if not name or not re.fullmatch(r"[A-Za-z0-9_$]+", name):
        raise ValueError(f"Refusing unsafe Snowflake identifier: {name!r}")
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def list_dir_from_config(
    args: dict[str, Any],
    secrets: dict[str, Any],
    prefix: str | None = None,
) -> list[StorageEntry]:
    """List one level of a Snowflake account as a folder hierarchy.

    The warehouse "folders" are the three-level Snowflake namespace:

    * ``prefix`` empty / ``None`` → list **databases** (``dir``).
    * ``prefix == "<db>"`` → list **schemas** in that database (``dir``).
    * ``prefix == "<db>/<schema>"`` → list **tables** + **views** in
      that schema (``table``).

    Uses ``SHOW`` metadata commands (no row scans, negligible cost).
    The ``snowflake-connector-python`` import is lazy so this module
    stays import-safe.

    Args:
        args: Non-secret connection body.
        secrets: Credential dict.
        prefix: ``None`` / ``""`` | ``"<db>"`` | ``"<db>/<schema>"``.

    Returns:
        A list of :class:`~ovid2.drivers.base.StorageEntry`.

    Raises:
        ValueError: When ``prefix`` has more than two segments or an
            identifier fails the safety check.
        Exception: Vendor connect / query errors propagate to the
            caller (``storage_browse_svc`` maps them to a friendly
            ``message`` and never 500s).
    """
    import snowflake.connector as sf  # type: ignore[import-not-found]

    parts = [p for p in (prefix or "").split("/") if p]
    if len(parts) > 2:
        raise ValueError(
            "Snowflake browse depth is database/schema/table; "
            f"got prefix {prefix!r}."
        )

    kwargs = build_connect_kwargs(args, secrets)
    conn = sf.connect(**kwargs)  # type: ignore[no-untyped-call]
    try:
        cursor = conn.cursor()
        try:
            if not parts:
                return _list_databases(cursor)
            if len(parts) == 1:
                return _list_schemas(cursor, parts[0])
            return _list_tables(cursor, parts[0], parts[1])
        finally:
            cursor.close()
    finally:
        conn.close()


def _show_names(cursor: Any, statement: str, name_column: str) -> list[str]:
    """Run a ``SHOW`` statement and return the ``name_column`` values.

    ``SHOW`` results expose columns by name; we read the requested
    column by consulting ``cursor.description`` so the helper is
    resilient to Snowflake column-order changes.
    """
    cursor.execute(statement)
    rows = cursor.fetchall()
    columns = [str(d[0]).lower() for d in (cursor.description or [])]
    try:
        idx = columns.index(name_column.lower())
    except ValueError:
        idx = 1  # ``SHOW`` puts ``name`` second (after ``created_on``).
    names: list[str] = []
    for row in rows:
        if idx < len(row) and row[idx]:
            names.append(str(row[idx]))
    return names


def _list_databases(cursor: Any) -> list[StorageEntry]:
    """Return the account's databases as ``dir`` entries."""
    names = _show_names(cursor, "SHOW DATABASES", "name")
    entries: list[StorageEntry] = []
    for name in sorted(names):
        if name.upper() in _HIDDEN_DATABASES:
            continue
        entries.append(StorageEntry(name=name, kind="dir", path=name))
    return entries


def _list_schemas(cursor: Any, database: str) -> list[StorageEntry]:
    """Return the schemas in ``database`` as ``dir`` entries."""
    stmt = f"SHOW SCHEMAS IN DATABASE {_quote_identifier(database)}"
    names = _show_names(cursor, stmt, "name")
    entries: list[StorageEntry] = []
    for name in sorted(names):
        if name.upper() in _HIDDEN_SCHEMAS:
            continue
        entries.append(
            StorageEntry(name=name, kind="dir", path=f"{database}/{name}")
        )
    return entries


def _list_tables(cursor: Any, database: str, schema: str) -> list[StorageEntry]:
    """Return the tables + views in ``database.schema`` as ``table`` entries."""
    qualified = f"{_quote_identifier(database)}.{_quote_identifier(schema)}"
    names: set[str] = set()
    for keyword in ("TABLES", "VIEWS"):
        stmt = f"SHOW {keyword} IN SCHEMA {qualified}"
        try:
            for name in _show_names(cursor, stmt, "name"):
                names.add(name)
        except Exception as exc:  # noqa: BLE001 — a schema with no views still lists tables
            logger.debug("SHOW %s failed for %s: %s", keyword, qualified, exc)
            continue
    return [
        StorageEntry(
            name=name, kind="table", path=f"{database}/{schema}/{name}"
        )
        for name in sorted(names)
    ]


__all__ = [
    "_pem_to_der",
    "_read_passphrase",
    "_read_private_key_pem",
    "_resolve_credentials_dir",
    "build_connect_kwargs",
    "list_dir_from_config",
    "probe_auth_from_config",
]
