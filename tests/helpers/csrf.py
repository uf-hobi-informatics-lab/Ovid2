"""Test helper: a CSRF header bound to a ``TestClient``'s current session."""

from __future__ import annotations

from typing import Any


def csrf_headers_for(client: Any) -> dict[str, str]:
    """Return ``{X-CSRF-Token: …}`` bound to ``client``'s session cookie.

    CSRF tokens are session-bound (``ovid2.web.auth.csrf``), so a test must
    mint the token for the same session its client presents. Without a
    (valid) session cookie the token is the anonymous one.
    """
    from itsdangerous import BadSignature

    from ovid2.web.auth import cookie, csrf
    from ovid2.web.services import settings_svc

    raw = client.cookies.get(settings_svc.get_settings().auth.cookie_name)
    session_id = None
    if raw:
        try:
            session_id = cookie._serializer().loads(raw)
        except BadSignature:
            session_id = None
    return {
        csrf.CSRF_HEADER_NAME: csrf.issue_token(session_id if isinstance(session_id, str) else None)
    }


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Imported by tests.")
