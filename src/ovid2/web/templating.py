"""Jinja2 template configuration.

Centralised so route modules can import a single ``templates`` object.
Templates are packaged with the wheel via a ``force-include`` entry in
``pyproject.toml``.

Also registers the web tier's custom Jinja filters. Timestamps are
*stored* everywhere as UTC ISO-8601 strings (e.g.
``2026-08-21T03:13:38.870967+00:00``); the ``localtime`` /
``localdatetime`` / ``localtime_only`` filters convert them to the
operator's configured display timezone (``settings.ui.display_timezone``,
default ``America/New_York``) and render them in a compact, human-
readable form (``2026-08-21 08:13 PM EDT``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi.templating import Jinja2Templates

if TYPE_CHECKING:
    from ovid2.web.auth.deps import AuthContext

_WEB_ROOT: Path = Path(__file__).resolve().parent
_TEMPLATES_DIR: Path = _WEB_ROOT / "templates"
_STATIC_DIR: Path = _WEB_ROOT / "static"

templates: Jinja2Templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _resolve_tz(tz: str | None) -> ZoneInfo:
    """Return a ``ZoneInfo`` for ``tz`` or the configured display tz.

    When ``tz`` is falsy, read ``settings.ui.display_timezone`` from the
    live settings (default ``America/New_York``). Any unresolvable name
    degrades gracefully to UTC so a template render can never 500 on a
    bad timezone.
    """
    name = (tz or "").strip()
    if not name:
        try:
            # Imported lazily to avoid a circular import at module load
            # (settings_svc imports models which is import-cheap, but the
            # service also touches the DB path helpers).
            from ovid2.web.services import settings_svc

            name = settings_svc.get_settings().ui.display_timezone
        except Exception:  # noqa: BLE001 — display must never break render
            name = "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _parse_iso(value: object) -> datetime | None:
    """Best-effort parse of a stored UTC ISO-8601 timestamp.

    Accepts a ``datetime`` (returned as-is, assumed UTC when naive) or an
    ISO-8601 string (with optional trailing ``Z``). Returns ``None`` for
    empty / unparseable input so the calling filter can fall back to the
    raw value.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def localdatetime(value: object, tz: str | None = None) -> str:
    """Render a stored UTC timestamp as ``YYYY-MM-DD hh:mm AM/PM``.

    Example: ``2026-08-21T03:13:38.870967+00:00`` →
    ``2026-08-20 11:13 PM`` (America/New_York).

    The timezone abbreviation is intentionally omitted — operators
    found the trailing ``EDT`` / ``EST`` noisy and the display timezone
    is a fixed per-deployment setting anyway.

    Unparseable input is returned unchanged so mtimes / non-timestamp
    strings pass through harmlessly.
    """
    dt = _parse_iso(value)
    if dt is None:
        return "" if value is None else str(value)
    local = dt.astimezone(_resolve_tz(tz))
    return local.strftime("%Y-%m-%d %I:%M %p").replace(" 0", " ", 1)


def localtime_full(value: object, tz: str | None = None) -> str:
    """Like :func:`localdatetime` but with seconds.

    Example → ``2026-08-20 11:13:38 PM``.
    """
    dt = _parse_iso(value)
    if dt is None:
        return "" if value is None else str(value)
    local = dt.astimezone(_resolve_tz(tz))
    return local.strftime("%Y-%m-%d %I:%M:%S %p")


def localtime_only(value: object, tz: str | None = None) -> str:
    """Render only the wall-clock time-of-day (``hh:mm:ss AM/PM``).

    Used for dense event streams where the date is redundant.
    """
    dt = _parse_iso(value)
    if dt is None:
        return "" if value is None else str(value)
    local = dt.astimezone(_resolve_tz(tz))
    return local.strftime("%I:%M:%S %p")


def localtime_pretty(value: object, tz: str | None = None) -> str:
    """Render a stored UTC timestamp as ``<Mon> <D><ordinal>, <h>:<mm> <AM/PM>``.

    Example: ``2026-09-06T20:30:19+00:00`` → ``Sep 6th, 4:30 PM``
    (America/New_York). The year is intentionally omitted — this filter
    is used on the run-detail console's "Submitted" line where the run
    is unambiguously recent.

    Unparseable input is returned unchanged so non-timestamp strings
    pass through harmlessly.
    """
    dt = _parse_iso(value)
    if dt is None:
        return "" if value is None else str(value)
    local = dt.astimezone(_resolve_tz(tz))
    day = local.day
    # Ordinal suffix — the 11/12/13 teens always take "th".
    if 11 <= (day % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    time_part = local.strftime("%I:%M %p").lstrip("0")
    return f"{local.strftime('%b')} {day}{suffix}, {time_part}"


# ``localtime`` is the general-purpose alias used across templates.
templates.env.filters["localtime"] = localdatetime
templates.env.filters["localdatetime"] = localdatetime
templates.env.filters["localtime_full"] = localtime_full
templates.env.filters["localtime_only"] = localtime_only
templates.env.filters["localtime_pretty"] = localtime_pretty


def _current_ctx(request: object) -> AuthContext | None:
    """Resolve the current :class:`AuthContext` for a template render.

    Wrapped defensively: a resolution failure degrades to ``None`` (an
    unauthenticated view) rather than 500-ing the page. When
    ``settings.auth.enabled`` is ``False`` this returns the synthetic
    :data:`~ovid2.web.auth.roles.Role.SYSTEM_ADMIN` bypass context, so
    every capability check below answers ``True`` and today's fully-open
    nav renders unchanged.

    Args:
        request: The Starlette request passed into the template context.

    Returns:
        The resolved ``AuthContext`` or ``None``.
    """
    from starlette.requests import Request

    from ovid2.web.auth import deps

    if not isinstance(request, Request):
        return None
    try:
        return deps.current_user(request)
    except Exception:  # noqa: BLE001 — display must never break render.
        return None


def has_cap(request: object, capability: str) -> bool:
    """Return whether the current request's principal holds ``capability``.

    Registered as a Jinja global so templates can gate UI with
    ``{% if has_cap(request, 'manage_secrets') %}``. ``capability`` is the
    string value of a :class:`~ovid2.web.auth.roles.Capability` member
    (e.g. ``"view_public"``, ``"manage_secrets"``). An unknown capability
    name or an unresolved user yields ``False`` (hide the item).

    Args:
        request: The Starlette request from the template context.
        capability: The capability's string value.

    Returns:
        ``True`` iff a principal is resolved and its role holds the named
        capability.
    """
    from ovid2.web.auth.roles import Capability, has_capability

    ctx = _current_ctx(request)
    if ctx is None:
        return False
    try:
        cap = Capability(capability)
    except ValueError:
        return False
    return has_capability(ctx.role, cap)


def current_user_ctx(request: object) -> AuthContext | None:
    """Return the current principal's ``AuthContext`` for the user chip.

    Registered as a Jinja global so ``base.html`` can render the topbar
    user chip (email + role) and decide whether to show the Logout link.
    Returns ``None`` for an unresolved user.

    Args:
        request: The Starlette request from the template context.

    Returns:
        The resolved ``AuthContext`` or ``None``.
    """
    return _current_ctx(request)


def _auth_enabled() -> bool:
    """Return whether ``settings.auth.enabled`` is set (for the footer text).

    Degrades to ``False`` on any settings-read failure so a bad settings
    file cannot break the page chrome.
    """
    try:
        from ovid2.web.services import settings_svc

        return bool(settings_svc.get_settings().auth.enabled)
    except Exception:  # noqa: BLE001 — display must never break render.
        return False


def _register_globals() -> None:
    """Register template globals that must be imported lazily.

    :mod:`ovid2.web.auth.csrf` and :mod:`ovid2.web.auth.deps` import the
    session-cookie / settings / user services, which in turn touch the DB
    path helpers; importing them at this module's top level would risk an
    import cycle (route modules import ``templates`` early). Registering
    here — called at module bottom, after ``templates`` is fully built —
    mirrors how the filters above are wired and defers those imports until
    the interpreter has finished loading the web package graph.
    """
    from ovid2.web.auth import csrf

    csrf.register_csrf(templates)
    templates.env.globals["has_cap"] = has_cap
    templates.env.globals["current_user_ctx"] = current_user_ctx
    templates.env.globals["auth_enabled"] = _auth_enabled


_register_globals()


def templates_dir() -> Path:
    """Return the absolute path to the packaged templates directory."""
    return _TEMPLATES_DIR


def static_dir() -> Path:
    """Return the absolute path to the packaged static directory."""
    return _STATIC_DIR
