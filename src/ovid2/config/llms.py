"""JSON-driven LLM connection loader + field-spec catalogue (L1).

Ovid2 talks to two *roles* of large language model:

- **general** — a broad conversational model served by any
  OpenAI-compatible endpoint (OpenAI, vLLM, NIM, LiteLLM, Ollama, …).
  It answers the "secondary" questions System 1 does not own.
- **system1** — a fast decision model (e.g. JEV or a similar decision
  model). It is asked
  first and returns a structured decision with a confidence; when it
  abstains or is below threshold the router falls back to *general*.

Each LLM connection is one JSON file. The schema is deliberately
provider-shaped (``provider`` selects the wire protocol) and role-shaped
(``role`` declares which slot the connection is meant to fill), so a
new provider (JEV) only adds a field-spec entry here plus a provider
module under :mod:`ovid2.llm.providers` in the executor tier.

This module is **web-safe**: it imports no HTTP client and no provider
module. The web tier uses it for listing, form rendering and
validation; secret resolution (:func:`resolve_secrets`) is only ever
called from the executor (``ovid2 tools llm-chat`` / ``test-llm``),
enforced by ``tests/contract/test_web_no_plaintext_secret_reads.py``.

Config file resolution order (identical to engines / connections):

1. ``$OVID2_LLMS_DIR/<name>.json``
2. ``$XDG_CONFIG_HOME/ovid2/llms/<name>.json``
3. ``./llms/<name>.json`` (repo-local convenience for dev)
4. ``/etc/ovid2/llms/<name>.json``

On-disk shape::

    {"provider": "openai_compatible",
     "role": "general",
     "args": {"base_url": "...", "model": "...", ...},
     "secret_ref": "llm_<name>"}
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ovid2.config import secrets as _secrets_module

#: LLM roles. ``general`` answers open questions; ``system1`` makes
#: fast structured decisions and is consulted first by the router.
ROLES: tuple[str, ...] = ("general", "system1")

#: Provider names known to the web layer. Mirrors
#: :data:`ovid2.llm.providers.PROVIDER_TYPE_MAP`; a contract test keeps
#: the two in sync.
_KNOWN_PROVIDERS: tuple[str, ...] = ("openai_compatible", "jev")

#: Which roles each provider is able to fill. ``jev`` is a System 1
#: provider; an OpenAI-compatible endpoint can serve either role (a
#: general model can be prompted to emit decisions in a pinch).
_PROVIDER_ROLES: dict[str, tuple[str, ...]] = {
    "openai_compatible": ("general", "system1"),
    "jev": ("system1",),
}

_COMMON_TUNING: list[dict[str, Any]] = [
    {"name": "timeout_s", "required": False, "default": "60",
     "help": "Per-request timeout in seconds."},
    {"name": "max_tokens", "required": False, "default": "2048",
     "help": "Upper bound on generated tokens per reply."},
    {"name": "temperature", "required": False, "default": "0.2",
     "help": "Sampling temperature (0 = deterministic)."},
    {"name": "system_prompt", "required": False,
     "help": "Optional system prompt prepended to every conversation.",
     "multiline": True},
]

_FIELD_SPECS: dict[str, dict[str, Any]] = {
    "openai_compatible": {
        "args": [
            {"name": "base_url", "required": True,
             "help": "OpenAI-compatible API root (ends in /v1), e.g. "
                     "https://api.openai.com/v1 or http://localhost:11434/v1."},
            {"name": "model", "required": True,
             "help": "Model id exactly as the endpoint lists it "
                     "(GET /v1/models)."},
            *_COMMON_TUNING,
            {"name": "context_window", "required": False, "default": "128000",
             "help": "Context window in tokens; older turns are trimmed to fit."},
            {"name": "ca_bundle", "required": False,
             "help": "Optional path to a CA bundle for TLS-intercepting proxies."},
        ],
        "secrets": [
            {"name": "api_key", "required": True,
             "help": "Endpoint API key (sent as 'Authorization: Bearer ...'). "
                     "Use a service-account key scoped to this model."},
        ],
        "note": ("Works with any OpenAI-compatible /v1/chat/completions "
                 "endpoint: OpenAI, vLLM, NIM, LiteLLM, Ollama, or an "
                 "institutional AI gateway."),
    },
    "jev": {
        "args": [
            {"name": "base_url", "required": True,
             "help": "JEV decision endpoint root (provided when JEV ships)."},
            {"name": "model", "required": False,
             "help": "JEV model / policy id, if the endpoint hosts several."},
            {"name": "confidence_threshold", "required": False, "default": "0.7",
             "help": "Decisions below this confidence fall back to the "
                     "general LLM."},
            {"name": "timeout_s", "required": False, "default": "15",
             "help": "Per-request timeout in seconds (System 1 should be fast)."},
        ],
        "secrets": [
            {"name": "api_key", "required": False,
             "help": "JEV API key / token (if the endpoint requires one)."},
        ],
        "note": ("JEV (System 1) is not available yet: the connection can be "
                 "saved now, and probing/chat will report 'not_implemented' "
                 "until the JEV provider lands. The router then falls back "
                 "to the general LLM."),
    },
}


def list_providers() -> list[str]:
    """Return the sorted list of registered LLM provider names."""
    return sorted(_KNOWN_PROVIDERS)


def provider_roles(provider: str) -> tuple[str, ...]:
    """Return the roles a provider may fill (empty when unknown)."""
    return _PROVIDER_ROLES.get(provider, ())


def llm_field_spec(provider: str) -> dict[str, Any]:
    """Return the ``{args, secrets, note}`` field spec for a provider.

    Args:
        provider: One of :data:`_KNOWN_PROVIDERS`.

    Returns:
        Shallow-copied dict with ``args`` / ``secrets`` lists, a ``note``
        string and a ``roles`` list.

    Raises:
        KeyError: When ``provider`` is unknown.
    """
    if provider not in _FIELD_SPECS:
        raise KeyError(
            f"No field spec for LLM provider '{provider}'. "
            f"Registered: {sorted(_FIELD_SPECS)}."
        )
    spec = _FIELD_SPECS[provider]
    return {
        "args": [dict(f) for f in spec.get("args", [])],
        "secrets": [dict(f) for f in spec.get("secrets", [])],
        "note": str(spec.get("note", "")),
        "roles": list(provider_roles(provider)),
    }


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _search_paths() -> list[Path]:
    """Return the ordered list of directories to search for LLM JSONs."""
    paths: list[Path] = []
    override = os.environ.get("OVID2_LLMS_DIR")
    if override:
        paths.append(Path(override))
    paths.append(llms_dir())
    paths.append(Path.cwd() / "llms")
    paths.append(Path("/etc/ovid2/llms"))
    return paths


def llms_dir() -> Path:
    """Return the user-scoped write target for LLM connection files."""
    override = os.environ.get("OVID2_LLMS_DIR")
    if override:
        return Path(override)
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg_cfg) / "ovid2" / "llms"


def locate_llm(name: str) -> Path | None:
    """Return the on-disk path of an LLM JSON, or ``None`` when absent."""
    for directory in _search_paths():
        candidate = directory / f"{name}.json"
        if candidate.is_file():
            return candidate
    return None


def list_available_llms() -> list[str]:
    """Return every LLM connection name discoverable on the search path."""
    seen: set[str] = set()
    for directory in _search_paths():
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if (entry.is_file() and entry.suffix == ".json"
                    and not entry.stem.endswith("_sample")):
                seen.add(entry.stem)
    return sorted(seen)


# ---------------------------------------------------------------------------
# Validation + loading
# ---------------------------------------------------------------------------


def validate_llm_doc(name: str, config: Any) -> dict[str, Any]:
    """Validate a parsed LLM connection document.

    Args:
        name: Connection name (for error messages).
        config: Parsed JSON body.

    Returns:
        The same dict with ``args`` defaulted to ``{}``.

    Raises:
        ValueError: On a malformed document, unknown provider / role, or
            secret material stored inline in ``args``.
    """
    if not isinstance(config, dict):
        raise ValueError(f"LLM config '{name}' must be a JSON object.")
    provider = config.get("provider")
    if provider not in _KNOWN_PROVIDERS:
        raise ValueError(
            f"Unknown LLM provider '{provider}' in '{name}'. "
            f"Registered: {sorted(_KNOWN_PROVIDERS)}."
        )
    role = config.get("role", "general")
    if role not in ROLES:
        raise ValueError(f"Unknown LLM role '{role}' in '{name}'. Known: {list(ROLES)}.")
    if role not in provider_roles(str(provider)):
        raise ValueError(
            f"Provider '{provider}' cannot fill role '{role}' "
            f"(allowed: {list(provider_roles(str(provider)))})."
        )
    config["role"] = role
    config.setdefault("args", {})
    if not isinstance(config["args"], dict):
        raise ValueError(f"LLM config '{name}' has non-object 'args'.")
    secret_names = {f["name"] for f in _FIELD_SPECS[str(provider)]["secrets"]}
    inline = sorted(set(config["args"]) & secret_names)
    if inline:
        raise ValueError(
            f"LLM config '{name}' has secret fields inline in 'args': {inline}. "
            "Move them into a secret file and set 'secret_ref'."
        )
    return config


def load_llm_config(name: str) -> dict[str, Any]:
    """Load and validate an LLM connection JSON by name.

    Raises:
        FileNotFoundError: When no search directory contains the file.
        ValueError: When the document is invalid.
    """
    path = locate_llm(name)
    if path is None:
        tried = ", ".join(str(d) for d in _search_paths())
        raise FileNotFoundError(f"LLM config '{name}.json' not found in any of: {tried}")
    with path.open(encoding="utf-8") as fh:
        config = json.load(fh)
    return validate_llm_doc(name, config)


def resolve_secrets(config: dict[str, Any]) -> dict[str, Any]:
    """Return the secret payload referenced by ``config['secret_ref']``.

    **Executor tier only.** Never call from ``ovid2.web``.
    """
    ref = config.get("secret_ref")
    if not ref:
        return {}
    return _secrets_module.load_secret(str(ref))


__all__ = [
    "ROLES",
    "list_available_llms",
    "list_providers",
    "llm_field_spec",
    "llms_dir",
    "load_llm_config",
    "locate_llm",
    "provider_roles",
    "resolve_secrets",
    "validate_llm_doc",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.config.llms is a library module.")
