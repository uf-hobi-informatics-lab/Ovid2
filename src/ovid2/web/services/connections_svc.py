"""Connection editor service (W7).

Owns the CRUD path for connection files under
``$XDG_CONFIG_HOME/ovid2/connections/``. Connections describe *how to
reach a system* — an S3 bucket, a Snowflake account, a Postgres
server. Addresses view a location *inside* a connection.

Rules enforced here:

- Every mutation lands in the user-scoped connections directory.
  System-wide files under ``/etc/ovid2/connections`` are read-only
  through the web tier (matches the address / secret CRUD policy).
- Secret material is never persisted to the connection JSON.
  ``ConnectionWrite.secrets`` values are written to a mode-``0o600``
  file via :mod:`ovid2.web.services.secrets_svc`, and the connection
  file records the secret name in ``secret_ref`` only.
- Sensitive values echoed back from the UI as the sentinel ``***`` are
  preserved from the existing on-disk secret.
- Every write appends a ``web_audit`` row with pre/post content hashes.

The service reads connection metadata (field spec catalogue) from
:mod:`ovid2.config.connections`. It never imports pipeline code —
concrete connector classes live under ``ovid2.connections`` and
are only instantiated by the pipeline / ETL layer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from ovid2.config import connections as _connections
from ovid2.web import db as web_db
from ovid2.web.models import (
    ConnectionDetail,
    ConnectionFieldSpec,
    ConnectionSummary,
    ConnectionTestReport,
    ConnectionTypeSpec,
    ConnectionWrite,
    WriteResult,
)
from ovid2.web.services import secrets_svc
from ovid2.web.services._broker import BrokerError, run_tool_json

logger = logging.getLogger(__name__)

__all__ = [
    "SENTINEL_MASK",
    "TEST_DEADLINE_SECONDS",
    "ConnectionError",
    "delete_connection",
    "get_connection",
    "list_connection_types",
    "list_connections",
    "test_connection",
    "write_connection",
]

SENTINEL_MASK: str = "***"

#: Hard wall-clock ceiling on any single connection probe. Enforced by
#: :func:`test_connection` on top of whatever per-request timeout the
#: underlying SDK offers, so a buggy driver cannot wedge the web UI.
#: Individual connectors are still expected to fail fast (< 10 s) —
#: this ceiling only kicks in when they don't.
TEST_DEADLINE_SECONDS: float = 20.0

_SAMPLE_SUFFIX: str = "_sample"


class ConnectionError(ValueError):
    """Raised when a connection payload is invalid or unsafe."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_name(name: str) -> None:
    """Reject filename stems that would escape the connections directory."""
    if not name:
        raise ConnectionError("Connection name is required.")
    if not all(c.isalnum() or c in "-_." for c in name):
        raise ConnectionError(
            f"Invalid connection name '{name}'. Use letters, digits, '-', '_', '.' only."
        )
    if name.startswith(".") or name.endswith(_SAMPLE_SUFFIX):
        raise ConnectionError(f"Connection name '{name}' is reserved.")


def _writable_path(name: str) -> Path:
    """Return the user-scoped write target for a connection JSON."""
    directory = _connections.connections_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{name}.json"


def _hash(payload: dict[str, Any]) -> str:
    """Return a stable content hash for audit rows."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _default_secret_ref(name: str) -> str:
    """Return the default secret-file stem for a connection with no ``secret_ref``."""
    return f"conn_{name}"


def _validate_args_against_spec(
    type_name: str, args: dict[str, Any], secrets: dict[str, Any]
) -> None:
    """Check that required fields are present and no field is misplaced."""
    spec = _connections.connection_field_spec(type_name)
    known_args = {f["name"] for f in spec["args"]}
    known_secrets = {f["name"] for f in spec["secrets"]}
    for f in spec["args"]:
        if f.get("required") and not args.get(f["name"]):
            raise ConnectionError(f"'{type_name}' connections require the '{f['name']}' argument.")
    misplaced = sorted(set(args) & known_secrets)
    if misplaced:
        raise ConnectionError(
            f"Fields {misplaced} are secrets for '{type_name}'. "
            "Move them into the 'secrets' payload."
        )
    unknown_args = sorted(set(args) - known_args)
    if unknown_args:
        raise ConnectionError(
            f"Unknown args {unknown_args} for '{type_name}'. Known: {sorted(known_args)}."
        )
    unknown_secrets = sorted(set(secrets) - known_secrets)
    if unknown_secrets:
        raise ConnectionError(
            f"Unknown secrets {unknown_secrets} for '{type_name}'. Known: {sorted(known_secrets)}."
        )


# ---------------------------------------------------------------------------
# List / get
# ---------------------------------------------------------------------------


def _read_raw(path: Path) -> dict[str, Any]:
    """Return the raw on-disk contents of a connection file."""
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ConnectionError(f"Connection file {path} is not a JSON object.")
    return data


def list_connections() -> list[ConnectionSummary]:
    """Return every connection config discoverable on the search path."""
    summaries: list[ConnectionSummary] = []
    for name in _connections.list_available_connections():
        path = _connections.locate_connection(name)
        if path is None:
            continue
        try:
            payload = _read_raw(path)
        except (OSError, json.JSONDecodeError, ConnectionError):
            continue
        summaries.append(
            ConnectionSummary(
                name=name,
                type=str(payload.get("type", "unknown")),
                path=str(path),
                secret_ref=(str(payload.get("secret_ref")) if payload.get("secret_ref") else None),
            )
        )
    return summaries


def get_connection(name: str) -> ConnectionDetail | None:
    """Return one connection with masked secret placeholders."""
    path = _connections.locate_connection(name)
    if path is None:
        return None
    try:
        payload = _read_raw(path)
    except (OSError, json.JSONDecodeError, ConnectionError):
        return None
    type_name = str(payload.get("type", "unknown"))
    args = dict(payload.get("args") or {})
    # Widen legacy disc shape ({"root_path": …}) into the new
    # {host_path, container_path, mount_mode} triple so the UI and
    # every downstream consumer see one canonical shape. Nothing on
    # disk is rewritten — legacy files keep their original keys.
    if type_name == "disc":
        from ovid2.drivers.disc import normalise_disc_args

        args = {**normalise_disc_args(args)}
    secret_ref = payload.get("secret_ref") or None
    masked_secrets: dict[str, str] = {}
    if secret_ref:
        detail = secrets_svc.get_secret(str(secret_ref))
        if detail is not None:
            masked_secrets = dict.fromkeys(detail.keys, SENTINEL_MASK)
    return ConnectionDetail(
        name=name,
        type=type_name,
        path=str(path),
        args=args,
        secret_ref=str(secret_ref) if secret_ref else None,
        secrets=masked_secrets,
        auth_mode=_maybe_auth_mode(name, type_name),
    )


def _maybe_auth_mode(name: str, type_name: str) -> str | None:
    """Return the connector's auth-mode label from key *names* only.

    Only ``azure_blob`` currently publishes an auth-mode label. We
    classify it from the *presence* of secret keys (never their values):
    a stored secret key is only ever written when it carries a value, so
    key-presence is a faithful proxy for the value-based classifier.
    This keeps the API tier free of any plaintext-secret read (harness /
    API / executor trust model) while still rendering the label.

    Never surfaces credential material: the returned label is one of a
    fixed set of tokens (``connection_string``, ``account_key``,
    ``sas_token``, ``service_principal``, ``default_chain``, …).
    """
    if type_name != "azure_blob":
        return None
    path = _connections.locate_connection(name)
    if path is None:
        return None
    try:
        payload = _read_raw(path)
    except (OSError, json.JSONDecodeError, ConnectionError):
        return None
    secret_ref = payload.get("secret_ref") or None
    if not secret_ref:
        return "default_chain"
    detail = secrets_svc.get_secret(str(secret_ref))
    if detail is None:
        return "default_chain"
    # Feed the classifier sentinel non-empty markers keyed by the secret
    # key *names* — no value ever leaves ``secrets_svc``.
    present = dict.fromkeys(detail.keys, "x")
    try:
        from ovid2.drivers.azure_blob.metadata import resolve_connection_auth_mode

        return str(resolve_connection_auth_mode(present))
    except Exception:
        return None


def list_connection_types() -> list[ConnectionTypeSpec]:
    """Return the field-spec catalogue for every registered connector.

    Used by ``GET /api/v1/connections/types`` — the UI (and MCP tools)
    consume this to render the dynamic form.
    """
    specs: list[ConnectionTypeSpec] = []
    for type_name in _connections.list_connection_types():
        spec = _connections.connection_field_spec(type_name)
        specs.append(
            ConnectionTypeSpec(
                type=type_name,
                args=[ConnectionFieldSpec(**f) for f in spec["args"]],
                secrets=[ConnectionFieldSpec(**f) for f in spec["secrets"]],
                note=spec.get("note"),
            )
        )
    return specs


# ---------------------------------------------------------------------------
# Write / delete
# ---------------------------------------------------------------------------


def _merge_secret_updates(ref: str | None, incoming: dict[str, Any]) -> dict[str, Any]:
    """Preserve on-disk secret values for keys sent as the mask sentinel."""
    existing: dict[str, Any] = {}
    if ref:
        try:
            existing_detail = secrets_svc.get_secret(ref)
            if existing_detail is not None:
                # ``get_secret`` returns keys only — we cannot recover
                # values from the API. Read the raw file the same way
                # secrets_svc does, one directory up.
                candidate = Path(existing_detail.path)
                if candidate.is_file():
                    try:
                        existing = json.loads(candidate.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        existing = {}
        except Exception:  # pragma: no cover — defensive
            existing = {}
    merged: dict[str, Any] = {}
    for key, value in incoming.items():
        if value == SENTINEL_MASK:
            existing_value = existing.get(key)
            if existing_value in (None, ""):
                raise ConnectionError(
                    f"Secret field '{key}' is masked but has no value on disk. "
                    "Enter the real value before saving."
                )
            merged[key] = existing_value
        else:
            merged[key] = value
    return merged


def write_connection(
    name: str,
    payload: ConnectionWrite,
    *,
    actor: str = "web",
    create_only: bool = False,
) -> WriteResult:
    """Create or update a connection JSON.

    Secret material lands in a separate mode-0o600 secret file. Only
    the reference (``secret_ref``) is stored in the connection JSON.

    Args:
        name: Filename stem.
        payload: Parsed :class:`ConnectionWrite` from the client.
        actor: Free-form origin label recorded in ``web_audit``.
        create_only: When True, refuse to overwrite an existing file.

    Returns:
        A :class:`WriteResult` describing the write.

    Raises:
        ConnectionError: On validation failure or a create-conflict.
    """
    _validate_name(name)
    type_name = str(payload.type)
    if type_name not in _connections.CONNECTION_TYPE_MAP:
        raise ConnectionError(
            f"Unknown connection type '{type_name}'. "
            f"Registered: {sorted(_connections.CONNECTION_TYPE_MAP.keys())}."
        )

    incoming_args = dict(payload.args or {})
    incoming_secrets = dict(payload.secrets or {})
    _validate_args_against_spec(type_name, incoming_args, incoming_secrets)

    existing_path = _connections.locate_connection(name)
    existing_raw: dict[str, Any] | None = None
    if existing_path is not None:
        if create_only:
            raise ConnectionError(f"Connection '{name}' already exists.")
        try:
            existing_raw = _read_raw(existing_path)
        except (OSError, json.JSONDecodeError, ConnectionError) as exc:
            raise ConnectionError(f"Existing config is not readable: {exc}") from exc

    secret_ref: str | None = None
    if incoming_secrets:
        secret_ref = str(
            payload.secret_ref
            or (existing_raw or {}).get("secret_ref")
            or _default_secret_ref(name)
        )
        # Merge sentinel values with the existing on-disk secret file.
        merged_secrets = _merge_secret_updates(secret_ref, incoming_secrets)
        # Delegate the write — secrets_svc enforces mode 0o600.
        from ovid2.web.models import SecretWrite  # local import to avoid loop

        try:
            secrets_svc.write_secret(
                secret_ref,
                SecretWrite(values=merged_secrets),
                actor=actor,
            )
        except secrets_svc.SecretError as exc:
            raise ConnectionError(f"Secret write failed: {exc}") from exc
    else:
        secret_ref = (
            str(payload.secret_ref)
            if payload.secret_ref
            else (
                str(existing_raw.get("secret_ref"))
                if existing_raw and existing_raw.get("secret_ref")
                else None
            )
        )

    doc: dict[str, Any] = {"type": type_name, "args": incoming_args}
    if secret_ref:
        doc["secret_ref"] = secret_ref

    target = _writable_path(name)
    before_hash = _hash(existing_raw) if existing_raw is not None else None
    after_hash = _hash(doc)
    action = "updated" if existing_raw is not None else "created"

    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(target)

    web_db.ensure_schema()
    web_db.audit(
        actor=actor,
        action=f"connection.{action}",
        target=name,
        before_hash=before_hash,
        after_hash=after_hash,
        detail_json=json.dumps({"path": str(target), "type": type_name}),
    )
    logger.info("Connection %s %s at %s", action, name, target)
    return WriteResult(name=name, path=str(target), action=action)


def delete_connection(name: str, *, actor: str = "web") -> WriteResult:
    """Delete a connection JSON (and audit the event).

    The referenced secret file is *not* removed automatically — an
    operator may reuse it under a different name. Deleting the secret
    is a separate ``DELETE /api/v1/secrets/{name}`` call.
    """
    _validate_name(name)
    target = _writable_path(name)
    if not target.is_file():
        raise ConnectionError(
            f"Connection '{name}' not found under {_connections.connections_dir()}."
        )
    before_hash: str | None = None
    try:
        before_hash = _hash(_read_raw(target))
    except (OSError, json.JSONDecodeError, ConnectionError):
        before_hash = None
    target.unlink()
    web_db.ensure_schema()
    web_db.audit(
        actor=actor,
        action="connection.deleted",
        target=name,
        before_hash=before_hash,
        detail_json=json.dumps({"path": str(target)}),
    )
    logger.info("Connection deleted %s at %s", name, target)
    return WriteResult(name=name, path=str(target), action="deleted")


# ---------------------------------------------------------------------------
# Test probe
# ---------------------------------------------------------------------------


def test_connection(name: str) -> ConnectionTestReport:
    """Probe a connection (delegates to the executor-brokered probe).

    Kept as a stable public name for the pre-D4 endpoint. The actual
    probe — which needs the connection's credentials — runs in a
    short-lived ``ovid2 tools probe-connection`` subprocess so this API
    process never resolves a plaintext secret. See
    :func:`test_connection_via_driver` and
    the adr-harness-api-executor-trust-model design note.
    """
    return test_connection_via_driver(name)


# ---------------------------------------------------------------------------
# D4 — Driver-based probe wrapper
# ---------------------------------------------------------------------------


def test_connection_via_driver(name: str) -> ConnectionTestReport:
    """Run the connection auth probe in the executor trust domain.

    In the harness / API / executor trust model this API process must
    not resolve plaintext secrets. The probe — which needs the
    connection's credentials — is delegated to a short-lived
    ``ovid2 tools probe-connection <name>`` subprocess (see
    :mod:`ovid2.cli.tools`). We only parse its JSON result here, so no
    secret value ever enters this long-lived process. See
    the adr-harness-api-executor-trust-model design note.
    """
    # Existence check needs no secret, so keep it in-process for a clean
    # early error rather than paying the subprocess cost.
    if _connections.locate_connection(name) is None:
        return ConnectionTestReport(
            ok=False,
            code="config",
            message=f"Connection '{name}' not found.",
            detail={},
        )

    try:
        result = run_tool_json(
            ["probe-connection", name],
            timeout=TEST_DEADLINE_SECONDS,
        )
    except BrokerError as exc:
        return ConnectionTestReport(
            ok=False,
            code="unreachable",
            message=f"Probe subprocess failed: {exc}",
            detail={},
        )

    return ConnectionTestReport(
        ok=bool(result.get("ok", False)),
        code=str(result.get("code", "error")),
        message=str(result.get("message", "")),
        detail=dict(result.get("detail") or {}),
    )
