"""Address service — list, read, create, update, delete (W4).

Wraps :mod:`ovid2.config.addresses` to enumerate every JSON config in the
XDG search path and to persist edits back to disk.

Design notes:

- Reads mask sensitive keys with the sentinel ``***`` before returning
  them to the UI/API. Writes accept the same sentinel back and preserve
  the existing on-disk value, so the round-trip is safe.
- Writes always land in ``$XDG_CONFIG_HOME/ovid2/configs/`` (the primary
  user-scoped location on the search path). Editing a config that lives
  in ``/etc/ovid2/configs/`` therefore creates a shadowing copy in the
  user config directory rather than mutating the system-wide file.
- Every mutation appends a row to ``web_audit`` with the pre- and
  post-image content hashes so operators can trace who changed what.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from ovid2.config import addresses as _addresses
from ovid2.config import paths as _paths
from ovid2.web import db as web_db
from ovid2.web.models import AddressDetail, AddressSummary, AddressWrite, WriteResult

logger = logging.getLogger(__name__)

__all__ = [
    "SENSITIVE_KEYS",
    "SENTINEL_MASK",
    "WAREHOUSE_CONNECTION_TYPES",
    "AddressError",
    "connection_type",
    "delete_address",
    "get_address",
    "is_legacy_shape",
    "is_warehouse_connection",
    "list_addresses",
    "resolve_or_create_address",
    "write_address",
]

#: Keys whose values are masked before being returned to the UI/API.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {"password", "passwd", "token", "secret", "private_key", "private_key_passphrase"}
)

#: Sentinel string echoed back for masked values. When a client sends
#: this string on ``PUT`` we treat it as "leave the existing value alone".
SENTINEL_MASK: str = "***"

#: Recognised address types. Matches :data:`ovid2.config.addresses.ADDRESS_TYPE_MAP`.
_VALID_SRC_NAMES: frozenset[str] = frozenset(_addresses.ADDRESS_TYPE_MAP.keys())

#: Connection types that address a *warehouse* (database.schema.table)
#: rather than a filesystem prefix. Unlike disc/s3, these have **no
#: runnable root**: a database (and usually a schema) is mandatory. An
#: empty location for one of these is a misconfiguration, not "the root".
WAREHOUSE_CONNECTION_TYPES: frozenset[str] = frozenset(
    {"snowflake", "postgres", "mssql", "databricks"}
)

#: Filename stems reserved for sample templates — filtered out of listings.
_SAMPLE_SUFFIX: str = "_sample"


def connection_type(connection: str) -> str | None:
    """Return the registered type of a connection config, or ``None``.

    Reads the referenced connection JSON and returns its ``type`` (e.g.
    ``snowflake``, ``disc``, ``s3``). Returns ``None`` when the connection
    cannot be located or parsed — callers treat an unknown type as
    non-warehouse (fail-open on classification, so a genuine filesystem
    connection is never blocked by a lookup miss).
    """
    try:
        from ovid2.config import connections as _connections

        conn_path = _connections.locate_connection(connection)
        if conn_path is None:
            return None
        with conn_path.open(encoding="utf-8") as fh:
            conn_payload = json.load(fh)
        return str(conn_payload.get("type", "")) or None
    except (OSError, json.JSONDecodeError):
        return None


def is_warehouse_connection(connection: str) -> bool:
    """Whether ``connection`` addresses a warehouse (no runnable root)."""
    return connection_type(connection) in WAREHOUSE_CONNECTION_TYPES


class AddressError(ValueError):
    """Raised when an address write is invalid or targets a bad name."""


def _mask(src_args: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of ``src_args`` with sensitive values redacted."""
    masked: dict[str, Any] = {}
    for key, value in src_args.items():
        if key.lower() in SENSITIVE_KEYS and value:
            masked[key] = SENTINEL_MASK
        else:
            masked[key] = value
    return masked


def _locate(name: str) -> Path | None:
    """Return the on-disk path of an address config, or ``None`` if missing."""
    for directory in _addresses._config_search_paths():
        candidate = directory / f"{name}.json"
        if candidate.is_file():
            return candidate
    return None


def _writable_path(name: str) -> Path:
    """Return the write target for an address config.

    Always resolves to the user-scoped ``configs_dir()`` — writes never
    mutate ``/etc/ovid2/configs`` (system-wide) even when a shadowing
    file lives there. Parent directories are created on demand.
    """
    directory = _paths.configs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{name}.json"


def _validate_name(name: str) -> None:
    """Reject filename stems that would escape the configs directory."""
    if not name:
        raise AddressError("Address name is required.")
    if not all(c.isalnum() or c in "-_." for c in name):
        raise AddressError(
            f"Invalid address name '{name}'. Use letters, digits, '-', '_', '.' only."
        )
    if name.startswith(".") or name.endswith(_SAMPLE_SUFFIX):
        raise AddressError(f"Address name '{name}' is reserved.")


def _hash(payload: dict[str, Any]) -> str:
    """Return a stable content hash for audit rows."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def list_addresses() -> list[AddressSummary]:
    """List every address config discoverable via the standard search path.

    Returns:
        Alphabetically-sorted list of :class:`AddressSummary`. Configs
        that fail to parse are silently skipped — the doctor page
        surfaces load errors separately.
    """
    summaries: list[AddressSummary] = []
    for name in _addresses.list_available_addresses():
        path = _locate(name)
        if path is None:
            continue
        try:
            with path.open(encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        # W7 connection-shape configs: derive src_name from the referenced
        # connection type via the loader's bridge map. When the connection
        # itself is missing we surface ``unknown`` rather than crash.
        connection_name = payload.get("connection")
        if connection_name:
            src_name = "unknown"
            try:
                # Lazy import to avoid a circular loop.
                from ovid2.config import connections as _connections

                path_conn = _connections.locate_connection(str(connection_name))
                if path_conn is not None:
                    with path_conn.open(encoding="utf-8") as fh:
                        conn_payload = json.load(fh)
                    conn_type = str(conn_payload.get("type", "unknown"))
                    src_name = _addresses.CONNECTION_TO_SRC_NAME.get(conn_type, "unknown")
            except Exception:
                src_name = "unknown"
            summaries.append(
                AddressSummary(
                    name=name,
                    src_name=src_name,
                    path=str(path),
                    connection=str(connection_name),
                )
            )
            continue
        src_name = str(payload.get("src_name", "unknown"))
        summaries.append(AddressSummary(name=name, src_name=src_name, path=str(path)))
    return summaries


def get_address(name: str) -> AddressDetail | None:
    """Return the parsed contents of one address config (with masked secrets).

    Args:
        name: Filename stem of the JSON config.

    Returns:
        :class:`AddressDetail` when the config exists and parses,
        ``None`` when the file is not on the search path.
    """
    path = _locate(name)
    if path is None:
        return None
    with path.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    # W7 connection-shape addresses expose ``connection`` + ``location``;
    # ``src_args`` is left empty so the UI knows to render the read-only
    # location fields instead of the full credential form.
    if "connection" in payload:
        from ovid2.config import connections as _connections

        connection_name = str(payload["connection"])
        location = dict(payload.get("location") or {})
        src_name = "unknown"
        try:
            conn_path = _connections.locate_connection(connection_name)
            if conn_path is not None:
                with conn_path.open(encoding="utf-8") as fh:
                    conn_payload = json.load(fh)
                src_name = _addresses.CONNECTION_TO_SRC_NAME.get(
                    str(conn_payload.get("type", "unknown")), "unknown"
                )
        except Exception:
            src_name = "unknown"
        return AddressDetail(
            name=name,
            src_name=src_name,
            path=str(path),
            src_args=_mask(location),
            connection=connection_name,
            location=location,
        )
    return AddressDetail(
        name=name,
        src_name=str(payload.get("src_name", "unknown")),
        path=str(path),
        src_args=_mask(dict(payload.get("src_args", {}))),
    )


def is_legacy_shape(detail: AddressDetail | dict[str, Any] | None) -> bool:
    """Return ``True`` when the address is stored in the pre-W7 shape.

    Legacy addresses embed credentials directly under ``src_args``.
    W7 addresses reference a :class:`Connection` and store only the
    location. The UI branches on this to render the correct edit form
    and to show the *"convert to connection"* banner on legacy rows.

    Args:
        detail: An :class:`AddressDetail`, a raw on-disk dict, or
            ``None``. ``None`` returns ``False`` (nothing to classify).

    Returns:
        ``True`` when the shape is legacy, ``False`` when it is a W7
        ``{connection, location}`` shape or the input is missing.
    """
    if detail is None:
        return False
    if isinstance(detail, AddressDetail):
        return detail.connection is None
    if isinstance(detail, dict):
        return "connection" not in detail
    return False


def _read_raw(path: Path) -> dict[str, Any]:
    """Return the raw on-disk contents of an address config (unmasked)."""
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise AddressError(f"Config file {path} is not a JSON object.")
    return data


def _merge_masked_updates(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any],
) -> dict[str, Any]:
    """Preserve existing sensitive values when the client sends the mask sentinel.

    Args:
        existing: The on-disk ``src_args`` (unmasked), or ``None`` for a
            new config.
        incoming: The ``src_args`` from the client payload. Values equal
            to :data:`SENTINEL_MASK` mean "keep whatever is on disk".

    Returns:
        The merged dict to persist. If a sentinel is sent for a key that
        does not exist on disk yet, we raise :class:`AddressError` so
        the operator does not silently save a config with an empty
        password.
    """
    merged: dict[str, Any] = {}
    for key, value in incoming.items():
        if value == SENTINEL_MASK:
            existing_value = (existing or {}).get(key)
            if not existing_value:
                raise AddressError(
                    f"Field '{key}' is masked but has no value on disk. "
                    "Enter the real value before saving."
                )
            merged[key] = existing_value
        else:
            merged[key] = value
    return merged


def _slugify_location(location: dict[str, Any]) -> str:
    """Return a short, filesystem-safe slug for an address ``location``.

    Prefers the most path-like location key (``path`` / ``prefix`` /
    ``database`` / ``bucket``) and falls back to the whole location's
    stringified values. Non-alphanumeric runs collapse to ``_``; the
    result is lower-cased and trimmed to 40 chars so the composed
    address name stays readable.
    """
    for key in ("path", "prefix", "database", "bucket", "container"):
        value = location.get(key)
        if value:
            raw = str(value)
            break
    else:
        raw = "_".join(str(v) for v in location.values() if v) or "root"
    slug = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").lower()
    slug = slug or "root"
    return slug[:40]


def _normalise_location(location: dict[str, Any] | None) -> dict[str, Any]:
    """Return a comparison-stable copy of a ``location`` dict.

    Drops keys whose value is ``None`` / empty string so two locations
    that differ only by an unset optional field compare equal.
    """
    result: dict[str, Any] = {}
    for key, value in (location or {}).items():
        if value in (None, ""):
            continue
        result[key] = value
    return result


def find_matching_address(connection: str, location: dict[str, Any] | None) -> str | None:
    """Return the name of an existing W7 address with identical shape.

    Scans every address config on the search path and returns the
    first whose ``{connection, location}`` matches (after
    :func:`_normalise_location`). ``None`` when no identical address
    exists. Legacy ``{src_name, src_args}`` configs never match.
    """
    target_conn = str(connection)
    target_loc = _normalise_location(location)
    for directory in _addresses._config_search_paths():
        if not directory.is_dir():
            continue
        for candidate in sorted(directory.glob("*.json")):
            try:
                raw = _read_raw(candidate)
            except (OSError, json.JSONDecodeError, AddressError):
                continue
            if str(raw.get("connection") or "") != target_conn:
                continue
            if _normalise_location(raw.get("location")) == target_loc:
                return candidate.stem
    return None


def resolve_or_create_address(
    connection: str,
    location: dict[str, Any] | None,
    *,
    actor: str = "web",
) -> str:
    """Return an address name for ``{connection, location}``, creating it if needed.

    Used by the submit-page folder browser to turn a chosen
    ``(storage account, folder)`` into a runnable address name without
    forcing the operator to pre-create one:

    1. If an existing W7 address already has the identical
       ``{connection, location}``, its name is **reused** (no
       duplicate is written).
    2. Otherwise a new address is created with the deterministic name
       ``<connection>__<slug-of-location>``. If that name is taken by
       a *different* shape, a numeric suffix is appended until a free
       name is found.

    Args:
        connection: Storage-account (connection) config name.
        location: Per-address location dict (``path`` / ``database`` /
            …). ``None`` is treated as an empty location.
        actor: Audit label forwarded to :func:`write_address`.

    Returns:
        The resolved address name (existing or freshly created).

    Raises:
        AddressError: When ``connection`` is empty, the write fails, or the
            connection is a warehouse (snowflake/postgres/mssql/databricks)
            resolved to an empty location (no database/schema).
    """
    if not connection:
        raise AddressError("A connection name is required to resolve an address.")

    norm_loc = _normalise_location(location)

    # A warehouse (snowflake/postgres/mssql/databricks) has no runnable
    # root: reading or writing requires a database (and usually a schema).
    # Refuse to resolve one to an empty location — otherwise the run
    # silently targets an empty database and every table read fails with
    # "No <warehouse> database specified", surfacing as a deceptively green
    # health summary. Filesystems (disc/s3) keep their valid root.
    if not norm_loc and is_warehouse_connection(connection):
        raise AddressError(
            f"Choose a database/schema for '{connection}' before submitting — "
            f"a warehouse connection has no runnable root."
        )

    reused = find_matching_address(connection, norm_loc)
    if reused is not None:
        return reused

    base = f"{connection}__{_slugify_location(norm_loc)}"
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", base).strip("_") or "address"
    name = base
    suffix = 2
    while _locate(name) is not None:
        # Name taken by a different shape (identical shapes were reused
        # above). Disambiguate deterministically.
        name = f"{base}_{suffix}"
        suffix += 1

    payload = AddressWrite(connection=connection, location=norm_loc or {})
    write_address(name, payload, actor=actor, create_only=True)
    return name


def write_address(
    name: str,
    payload: AddressWrite,
    *,
    actor: str = "web",
    create_only: bool = False,
) -> WriteResult:
    """Create or update an address config.

    Two supported shapes:

    - Legacy: populate :attr:`AddressWrite.src_name` +
      :attr:`AddressWrite.src_args`.
    - W7: populate :attr:`AddressWrite.connection` +
      :attr:`AddressWrite.location`. The write path validates that the
      named connection exists on the search path; it does *not*
      dereference secrets — those live in the connection's
      ``secret_ref`` and are managed by :mod:`secrets_svc`.

    Args:
        name: Filename stem.
        payload: Parsed :class:`AddressWrite` from the client.
        actor: Free-form origin label recorded in ``web_audit``.
        create_only: When True, refuse to overwrite an existing file.

    Returns:
        A :class:`WriteResult` describing the write.

    Raises:
        AddressError: On validation failure or a create-conflict.
    """
    _validate_name(name)

    existing_path = _locate(name)
    existing_raw: dict[str, Any] | None = None
    if existing_path is not None:
        if create_only:
            raise AddressError(f"Address '{name}' already exists.")
        try:
            existing_raw = _read_raw(existing_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise AddressError(f"Existing config is not readable: {exc}") from exc

    is_new_shape = bool(payload.connection or payload.location)
    is_legacy_shape = bool(payload.src_name or payload.src_args)
    if is_new_shape and is_legacy_shape:
        raise AddressError(
            "Address payload mixes shapes. Send either connection+location "
            "or src_name+src_args, not both."
        )
    if not is_new_shape and not is_legacy_shape:
        raise AddressError(
            "Address payload is empty. Provide src_name+src_args or connection+location."
        )

    if is_new_shape:
        doc = _build_new_shape_doc(name, payload)
    else:
        if create_only:
            # W7 flip: new addresses must use the connection shape.
            # Existing legacy files stay editable (round-trip) so the
            # 24 pre-W7 pipeline configs keep working, but the *point
            # of new entry* is closed. Migration path: `ovid2 tools
            # migrate-addresses` or the equivalent web wizard.
            raise AddressError(
                "Legacy-shape addresses are no longer creatable. "
                "Pick an existing connection and provide a location, "
                "or create a connection first at /connections/new."
            )
        doc = _build_legacy_shape_doc(payload, existing_raw)

    target = _writable_path(name)
    before_hash = _hash(existing_raw) if existing_raw is not None else None
    after_hash = _hash(doc)
    action = "updated" if existing_raw is not None else "created"

    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    tmp.chmod(0o644)
    tmp.replace(target)

    web_db.ensure_schema()
    web_db.audit(
        actor=actor,
        action=f"address.{action}",
        target=name,
        before_hash=before_hash,
        after_hash=after_hash,
        detail_json=json.dumps(
            {
                "path": str(target),
                "shape": "connection" if is_new_shape else "legacy",
            }
        ),
    )
    logger.info("Address %s %s at %s", action, name, target)
    return WriteResult(name=name, path=str(target), action=action)


def _build_new_shape_doc(name: str, payload: AddressWrite) -> dict[str, Any]:
    """Validate + assemble a ``{connection, location}`` document."""
    if not payload.connection:
        raise AddressError("connection is required when using the W7 shape.")
    from ovid2.config import connections as _connections

    if _connections.locate_connection(str(payload.connection)) is None:
        raise AddressError(f"Connection '{payload.connection}' is not on the config search path.")
    location = dict(payload.location or {})
    return {"connection": str(payload.connection), "location": location}


def _build_legacy_shape_doc(
    payload: AddressWrite, existing_raw: dict[str, Any] | None
) -> dict[str, Any]:
    """Validate + assemble a legacy ``{src_name, src_args}`` document."""
    if not payload.src_name:
        raise AddressError("src_name is required for legacy-shape writes.")
    if payload.src_name not in _VALID_SRC_NAMES:
        raise AddressError(
            f"Unknown src_name '{payload.src_name}'. Valid: {sorted(_VALID_SRC_NAMES)}."
        )
    existing_src_args = (
        dict(existing_raw.get("src_args", {}))
        if isinstance(existing_raw, dict) and existing_raw.get("src_args")
        else None
    )
    merged_src_args = _merge_masked_updates(existing_src_args, dict(payload.src_args or {}))
    if payload.src_name == "disc" and not merged_src_args.get("path"):
        raise AddressError("disc addresses require a non-empty 'path' argument.")
    if payload.src_name == "snowflake" and not merged_src_args.get("user"):
        raise AddressError("snowflake addresses require a 'user' argument at write time.")
    return {"src_name": payload.src_name, "src_args": merged_src_args}


def delete_address(name: str, *, actor: str = "web") -> WriteResult:
    """Delete an address config from the user-scoped ``configs_dir``.

    Files that only exist under ``/etc/ovid2/configs`` cannot be
    deleted through the web tier — the operator must remove them on
    disk. This mirrors the write policy (writes never touch system-wide
    paths).

    Args:
        name: Filename stem.
        actor: Audit label.

    Returns:
        :class:`WriteResult` with ``action='deleted'``.

    Raises:
        AddressError: When the file is missing from the user-scoped
            directory.
    """
    _validate_name(name)
    target = _writable_path(name)
    if not target.is_file():
        raise AddressError(f"Address '{name}' not found under {_paths.configs_dir()}.")
    before_hash: str | None = None
    try:
        before_hash = _hash(_read_raw(target))
    except (OSError, json.JSONDecodeError):
        before_hash = None
    target.unlink()
    web_db.ensure_schema()
    web_db.audit(
        actor=actor,
        action="address.deleted",
        target=name,
        before_hash=before_hash,
        detail_json=json.dumps({"path": str(target)}),
    )
    logger.info("Address deleted %s at %s", name, target)
    return WriteResult(name=name, path=str(target), action="deleted")
