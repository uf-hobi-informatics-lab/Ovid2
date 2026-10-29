"""Per-user appearance preferences (server-side, follows the user).

The Appearance panel (``static/appearance.js`` + ``static/theme.js``) and
the assistant's cosmetic UI actions edit four pieces of state:

* ``theme``        — base theme slug (``clinical``, ``financial``, …);
* ``colors``       — quick colour overrides ``{accent, bg, surface, fg}``;
* ``appearance``   — per-component CSS custom-property overrides;
* ``saved_themes`` — named snapshots ``{name, theme, overrides}``.

They are stored as one validated JSON blob per principal in the
``web_user_prefs`` table so the look follows the user to any browser or
laptop. Nothing about appearance is kept in the browser.

When ``settings.auth.enabled`` is false every caller is the synthetic
``anonymous`` principal, so the deployment shares a single profile.

Validation reuses the patterns declared on the ``set_appearance`` entry of
the agent-tool catalogue, which the contract suite already pins against the
browser's allow-lists, so server, browser and assistant cannot drift.

Writes are cosmetic and are deliberately not audited (same rule as the
assistant's UI actions, ``adr-llm-dual-system.md`` decision 10).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from ovid2.agent_tools import get_tool
from ovid2.web import db as web_db

MAX_SAVED_THEMES = 30
MAX_THEME_NAME = 60
_NAME_RE = re.compile(r"^[^<>]{1,60}$")
# Derived accent companions: the browser recomputes these from ``--accent``,
# but a stored map may carry them (older saved themes), so accept them.
_DERIVED_VARS: dict[str, re.Pattern[str]] = {
    "--accent-hover": re.compile(r"^#[0-9a-fA-F]{6}$"),
    "--accent-subtle": re.compile(r"^rgba\(\d{1,3}, \d{1,3}, \d{1,3}, (0|1|0?\.\d{1,3})\)$"),
    "--accent-fg": re.compile(r"^#[0-9a-fA-F]{6}$"),
}


class PrefsError(ValueError):
    """Raised when an appearance payload fails validation."""


@lru_cache(maxsize=1)
def _rules() -> tuple[frozenset[str], dict[str, re.Pattern[str]], dict[str, re.Pattern[str]]]:
    """Return ``(theme_slugs, colour_patterns, appearance_patterns)``.

    Derived from the ``set_appearance`` catalogue entry so there is exactly
    one declaration of what an appearance value may look like.
    """
    tool = get_tool("set_appearance")
    if tool is None:  # pragma: no cover - catalogue is bundled
        raise RuntimeError("set_appearance missing from the agent-tool catalogue")
    props = tool.params["properties"]
    themes = frozenset(props["theme"]["enum"])
    colors = {k: re.compile(v["pattern"]) for k, v in props["colors"]["properties"].items()}
    appearance = {k: re.compile(v["pattern"]) for k, v in props["appearance"]["properties"].items()}
    appearance.update(_DERIVED_VARS)
    return themes, colors, appearance


def default_appearance() -> dict[str, Any]:
    """Return the empty preference document (theme chosen by the browser)."""
    return {"theme": None, "colors": {}, "appearance": {}, "saved_themes": []}


def _check_map(value: Any, patterns: dict[str, re.Pattern[str]], what: str) -> dict[str, str]:
    """Validate a ``{key: string}`` map against per-key patterns."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PrefsError(f"{what} must be an object")
    out: dict[str, str] = {}
    for key, val in value.items():
        pat = patterns.get(key)
        if pat is None:
            raise PrefsError(f"unknown {what} key {key!r}")
        if not isinstance(val, str) or not pat.fullmatch(val):
            raise PrefsError(f"bad value for {what} {key!r}")
        out[key] = val
    return out


def _check_theme(value: Any, themes: frozenset[str], *, required: bool) -> str | None:
    """Validate a theme slug (``None`` allowed unless ``required``)."""
    if value is None and not required:
        return None
    if not isinstance(value, str) or value not in themes:
        raise PrefsError(f"unknown theme {value!r}")
    return value


def validate(payload: Any) -> dict[str, Any]:
    """Validate and normalise an appearance document.

    Args:
        payload: The decoded JSON body (``theme``, ``colors``,
            ``appearance``, ``saved_themes``; all optional).

    Returns:
        A normalised document containing exactly the four keys.

    Raises:
        PrefsError: When any field is malformed or unknown.
    """
    if not isinstance(payload, dict):
        raise PrefsError("appearance must be an object")
    unknown = set(payload) - {"theme", "colors", "appearance", "saved_themes"}
    if unknown:
        raise PrefsError(f"unknown field(s): {sorted(unknown)}")
    themes, colors, appearance = _rules()
    saved_in = payload.get("saved_themes") or []
    if not isinstance(saved_in, list) or len(saved_in) > MAX_SAVED_THEMES:
        raise PrefsError(f"saved_themes must be a list of at most {MAX_SAVED_THEMES}")
    saved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in saved_in:
        if not isinstance(entry, dict) or set(entry) - {"name", "theme", "overrides"}:
            raise PrefsError("each saved theme is {name, theme, overrides}")
        name = entry.get("name")
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name) or not name.strip():
            raise PrefsError("saved theme name must be 1-60 characters without < or >")
        if name in seen:
            raise PrefsError(f"duplicate saved theme {name!r}")
        seen.add(name)
        saved.append(
            {
                "name": name,
                "theme": _check_theme(entry.get("theme"), themes, required=True),
                "overrides": _check_map(entry.get("overrides"), appearance, "appearance"),
            }
        )
    return {
        "theme": _check_theme(payload.get("theme"), themes, required=False),
        "colors": _check_map(payload.get("colors"), colors, "colors"),
        "appearance": _check_map(payload.get("appearance"), appearance, "appearance"),
        "saved_themes": saved,
    }


_SCHEMA_READY: set[str] = set()


def _ensure_schema() -> None:
    """Run :func:`web_db.ensure_schema` once per DB path per process.

    ``get_appearance`` runs on every page render, so avoid re-issuing the
    full DDL set each time.
    """
    from ovid2.web.settings import web_db_path

    key = str(web_db_path())
    if key not in _SCHEMA_READY:
        web_db.ensure_schema()
        _SCHEMA_READY.add(key)


def get_appearance(user_id: str) -> dict[str, Any]:
    """Return the stored appearance for ``user_id`` (defaults when unset).

    A row that no longer validates (e.g. a theme was retired) degrades to
    the defaults rather than breaking every page render.
    """
    _ensure_schema()
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT appearance_json FROM web_user_prefs WHERE user_id = ?", (user_id,)
        ).fetchone()
    if row is None:
        return default_appearance()
    try:
        return validate(json.loads(row["appearance_json"]))
    except (PrefsError, ValueError):
        return default_appearance()


def save_appearance(user_id: str, payload: Any) -> dict[str, Any]:
    """Validate ``payload`` and store it as ``user_id``'s appearance.

    Returns:
        The normalised document that was stored.

    Raises:
        PrefsError: When the payload is invalid.
    """
    doc = validate(payload)
    _ensure_schema()
    with web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_user_prefs (user_id, appearance_json, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "appearance_json = excluded.appearance_json, updated_at = excluded.updated_at",
            (user_id, json.dumps(doc, sort_keys=True), datetime.now(UTC).isoformat()),
        )
    return doc
