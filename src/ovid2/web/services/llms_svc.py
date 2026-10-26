"""LLM connection service (L1) — CRUD, routing status, brokered probe.

Owns the files under ``$XDG_CONFIG_HOME/ovid2/llms/``. Mirrors
:mod:`ovid2.web.services.engines_svc`:

- Secret material (``api_key``) is never stored in the LLM JSON; it is
  written mode ``0o600`` to ``secrets.d/llm_<name>.json`` via
  :mod:`ovid2.web.services.secrets_svc` and referenced by ``secret_ref``.
- ``***`` sentinel values preserve the existing on-disk secret.
- Every mutation appends a ``web_audit`` row.
- Deleting a connection that ``settings.llm`` routes to is refused.
- The probe runs in an executor subprocess (``python -m ovid2.llm probe``),
  so this process never reads the API key.

Must not import :mod:`ovid2.llm` (``test_llm_tier_isolation.py``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from ovid2.config import llms as _llms
from ovid2.web import db as web_db
from ovid2.web.models import (
    LlmDetail,
    LlmFieldSpec,
    LlmProviderSpec,
    LlmStatus,
    LlmSummary,
    LlmTestReport,
    LlmWrite,
    SecretWrite,
    WriteResult,
)
from ovid2.web.services import llm_exec, secrets_svc

logger = logging.getLogger(__name__)

SENTINEL_MASK = "***"
_SAMPLE_SUFFIX = "_sample"
TEST_TIMEOUT_SECONDS = 30.0


class LlmError(ValueError):
    """Raised when an LLM payload is invalid or unsafe."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_name(name: str) -> None:
    if not name:
        raise LlmError("LLM connection name is required.")
    if not all(c.isalnum() or c in "-_." for c in name):
        raise LlmError(f"Invalid name '{name}'. Use letters, digits, '-', '_', '.' only.")
    if name.startswith(".") or name.endswith(_SAMPLE_SUFFIX):
        raise LlmError(f"Name '{name}' is reserved.")


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _read_raw(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise LlmError(f"LLM file {path} is not a JSON object.")
    return data


def _summary(name: str, path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    args = dict(payload.get("args") or {})
    return {
        "name": name,
        "provider": str(payload.get("provider", "unknown")),
        "role": str(payload.get("role", "general")),
        "model": str(args.get("model") or ""),
        "base_url": str(args.get("base_url") or ""),
        "path": str(path),
        "secret_ref": str(payload["secret_ref"]) if payload.get("secret_ref") else None,
    }


def _validate_against_spec(provider: str, args: dict[str, Any], secrets: dict[str, Any]) -> None:
    spec = _llms.llm_field_spec(provider)
    known_args = {f["name"] for f in spec["args"]}
    known_secrets = {f["name"] for f in spec["secrets"]}
    for f in spec["args"]:
        if f.get("required") and not str(args.get(f["name"]) or "").strip():
            raise LlmError(f"'{provider}' LLMs require the '{f['name']}' argument.")
    misplaced = sorted(set(args) & known_secrets)
    if misplaced:
        raise LlmError(f"Fields {misplaced} are secrets; send them under 'secrets'.")
    unknown = sorted(set(args) - known_args)
    if unknown:
        raise LlmError(f"Unknown args {unknown} for '{provider}'. Known: {sorted(known_args)}.")
    unknown_s = sorted(set(secrets) - known_secrets)
    if unknown_s:
        raise LlmError(f"Unknown secrets {unknown_s} for '{provider}'.")
    base_url = str(args.get("base_url") or "")
    if base_url and not base_url.startswith(("https://", "http://")):
        raise LlmError("base_url must start with https:// (or http:// for local endpoints).")


# ---------------------------------------------------------------------------
# Catalogue / list / get
# ---------------------------------------------------------------------------


def list_providers() -> list[LlmProviderSpec]:
    """Return the field-spec catalogue for every provider."""
    out: list[LlmProviderSpec] = []
    for provider in _llms.list_providers():
        spec = _llms.llm_field_spec(provider)
        out.append(
            LlmProviderSpec(
                provider=provider,
                roles=list(spec["roles"]),
                args=[LlmFieldSpec(**f) for f in spec["args"]],
                secrets=[LlmFieldSpec(**f) for f in spec["secrets"]],
                note=spec["note"],
            )
        )
    return out


def list_llms() -> list[LlmSummary]:
    """Return every LLM connection on the search path."""
    out: list[LlmSummary] = []
    for name in _llms.list_available_llms():
        path = _llms.locate_llm(name)
        if path is None:
            continue
        try:
            payload = _read_raw(path)
        except (OSError, json.JSONDecodeError, LlmError):
            continue
        out.append(LlmSummary(**_summary(name, path, payload)))
    return out


def get_llm(name: str) -> LlmDetail | None:
    """Return one LLM connection with masked secrets."""
    path = _llms.locate_llm(name)
    if path is None:
        return None
    try:
        payload = _read_raw(path)
    except (OSError, json.JSONDecodeError, LlmError):
        return None
    masked: dict[str, str] = {}
    ref = payload.get("secret_ref")
    if ref:
        detail = secrets_svc.get_secret(str(ref))
        if detail is not None:
            masked = dict.fromkeys(detail.keys, SENTINEL_MASK)
    return LlmDetail(
        **_summary(name, path, payload), args=dict(payload.get("args") or {}), secrets=masked
    )


# ---------------------------------------------------------------------------
# Write / delete
# ---------------------------------------------------------------------------


def write_llm(
    name: str, payload: LlmWrite, *, actor: str = "web", create_only: bool = False
) -> WriteResult:
    """Create or update an LLM connection JSON (+ its 0o600 secret file)."""
    _validate_name(name)
    provider = str(payload.provider)
    if provider not in _llms.list_providers():
        raise LlmError(f"Unknown LLM provider '{provider}'. Registered: {_llms.list_providers()}.")
    role = str(payload.role or "general")
    if role not in _llms.provider_roles(provider):
        raise LlmError(
            f"Provider '{provider}' cannot fill role '{role}'. "
            f"Allowed: {list(_llms.provider_roles(provider))}."
        )
    args = {
        k: v
        for k, v in dict(payload.args or {}).items()
        if not (isinstance(v, str) and not v.strip())
    }
    secrets = {
        k: v
        for k, v in dict(payload.secrets or {}).items()
        if not (isinstance(v, str) and not v.strip())
    }
    _validate_against_spec(provider, args, secrets)

    existing_path = _llms.locate_llm(name)
    existing_raw: dict[str, Any] | None = None
    if existing_path is not None:
        if create_only:
            raise LlmError(f"LLM connection '{name}' already exists.")
        try:
            existing_raw = _read_raw(existing_path)
        except (OSError, json.JSONDecodeError, LlmError) as exc:
            raise LlmError(f"Existing config is not readable: {exc}") from exc

    secret_ref = str(payload.secret_ref or (existing_raw or {}).get("secret_ref") or f"llm_{name}")
    required_secrets = [
        f["name"] for f in _llms.llm_field_spec(provider)["secrets"] if f.get("required")
    ]
    if secrets:
        # secrets_svc merges '***' sentinels with the on-disk values itself.
        try:
            secrets_svc.write_secret(secret_ref, SecretWrite(values=secrets), actor=actor)
        except secrets_svc.SecretError as exc:
            raise LlmError(f"Secret write failed: {exc}") from exc
    elif required_secrets and secrets_svc.get_secret(secret_ref) is None:
        raise LlmError(f"'{provider}' LLMs require: {required_secrets}.")

    doc: dict[str, Any] = {"provider": provider, "role": role, "args": args}
    if secrets or secrets_svc.get_secret(secret_ref) is not None:
        doc["secret_ref"] = secret_ref
    try:
        _llms.validate_llm_doc(name, json.loads(json.dumps(doc)))
    except ValueError as exc:
        raise LlmError(str(exc)) from exc

    directory = _llms.llms_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o755)
    target = directory / f"{name}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(target)

    action = "updated" if existing_raw is not None else "created"
    web_db.ensure_schema()
    web_db.audit(
        actor=actor,
        action=f"llm.{action}",
        target=name,
        before_hash=_hash(existing_raw) if existing_raw else None,
        after_hash=_hash(doc),
        detail_json=json.dumps({"path": str(target), "provider": provider, "role": role}),
    )
    logger.info("LLM connection %s %s at %s", action, name, target)
    return WriteResult(name=name, path=str(target), action=action)


def _references(name: str) -> list[str]:
    from ovid2.web.services import settings_svc

    llm = settings_svc.get_settings().llm
    refs = []
    if llm.general == name:
        refs.append("settings.llm.general")
    if llm.system1 == name:
        refs.append("settings.llm.system1")
    return refs


def delete_llm(name: str, *, actor: str = "web") -> WriteResult:
    """Delete an LLM connection JSON (its secret file is kept)."""
    _validate_name(name)
    target = _llms.llms_dir() / f"{name}.json"
    if not target.is_file():
        raise LlmError(f"LLM connection '{name}' not found under {_llms.llms_dir()}.")
    refs = _references(name)
    if refs:
        raise LlmError(f"LLM '{name}' is in use by {', '.join(refs)}. Change the routing first.")
    try:
        before = _hash(_read_raw(target))
    except (OSError, json.JSONDecodeError, LlmError):
        before = None
    target.unlink()
    web_db.ensure_schema()
    web_db.audit(
        actor=actor,
        action="llm.deleted",
        target=name,
        before_hash=before,
        detail_json=json.dumps({"path": str(target)}),
    )
    return WriteResult(name=name, path=str(target), action="deleted")


# ---------------------------------------------------------------------------
# Probe + status
# ---------------------------------------------------------------------------


def test_llm(name: str) -> LlmTestReport:
    """Probe an LLM connection in an executor subprocess."""
    if _llms.locate_llm(name) is None:
        raise LlmError(f"LLM connection '{name}' not found.")
    try:
        result = llm_exec.run_probe(name, timeout=TEST_TIMEOUT_SECONDS)
    except llm_exec.ExecError as exc:
        return LlmTestReport(ok=False, code="unreachable", message=str(exc))
    report = LlmTestReport(
        ok=bool(result.get("ok")),
        code=str(result.get("code", "error")),
        message=str(result.get("message", "")),
        detail=dict(result.get("detail") or {}),
    )
    web_db.ensure_schema()
    web_db.audit(
        actor="web",
        action="llm.tested",
        target=name,
        detail_json=json.dumps({"ok": report.ok, "code": report.code}),
    )
    return report


def status() -> LlmStatus:
    """Return the assistant's routing status for the chat panel."""
    from ovid2.web.services import settings_svc

    llm = settings_svc.get_settings().llm

    def model_of(conn: str | None) -> str | None:
        if not conn:
            return None
        detail = get_llm(conn)
        return detail.model if detail else None

    return LlmStatus(
        enabled=llm.enabled,
        general=llm.general,
        general_model=model_of(llm.general),
        system1=llm.system1,
        system1_model=model_of(llm.system1),
        confidence_threshold=llm.confidence_threshold,
        fallback_to_general=llm.fallback_to_general,
        tools_enabled=llm.tools_enabled,
    )


__all__ = [
    "SENTINEL_MASK",
    "LlmError",
    "delete_llm",
    "get_llm",
    "list_llms",
    "list_providers",
    "status",
    "test_llm",
    "write_llm",
]
