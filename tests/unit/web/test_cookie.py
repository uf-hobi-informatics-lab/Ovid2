"""Unit tests for the signed session-cookie manager (Wave 2, todo 8)."""

from __future__ import annotations

import stat
from http.cookies import SimpleCookie
from pathlib import Path

import pytest
from starlette.requests import Request
from starlette.responses import Response

from ovid2.web.auth import cookie as cookie_mod


def _make_request(
    *,
    scheme: str = "http",
    headers: list[tuple[bytes, bytes]] | None = None,
    cookies: dict[str, str] | None = None,
) -> Request:
    """Build a minimal Starlette ``Request`` for cookie tests."""
    header_list: list[tuple[bytes, bytes]] = list(headers or [])
    if cookies:
        jar = SimpleCookie()
        for name, value in cookies.items():
            jar[name] = value
        # SimpleCookie serialises one ``Set-Cookie``-style line per key; the
        # Cookie *request* header is a single ``k=v; k2=v2`` string.
        cookie_header = "; ".join(f"{k}={m.value}" for k, m in jar.items())
        header_list.append((b"cookie", cookie_header.encode("latin-1")))
    scope = {
        "type": "http",
        "method": "GET",
        "scheme": scheme,
        "path": "/",
        "query_string": b"",
        "headers": header_list,
        "server": ("testserver", 80),
    }
    return Request(scope)


def _set_cookie_lines(response: Response) -> list[str]:
    """Return every ``set-cookie`` header value on ``response``."""
    return [value.decode("latin-1") for key, value in response.raw_headers if key == b"set-cookie"]


@pytest.fixture(autouse=True)
def _reset_key_cache(isolated_xdg: Path):
    """Reset the in-process signing-key cache around each test."""
    cookie_mod.reset_signing_key_cache()
    yield
    cookie_mod.reset_signing_key_cache()


@pytest.mark.unit
def test_round_trip_issue_then_read(isolated_xdg: Path) -> None:
    """Issuing then reading a cookie returns the original session id."""
    response = Response()
    cookie_mod.issue_cookie(response, "session-abc-123")

    lines = _set_cookie_lines(response)
    assert len(lines) == 1
    jar = SimpleCookie()
    jar.load(lines[0])
    signed_value = jar["ovid2_session"].value

    request = _make_request(cookies={"ovid2_session": signed_value})
    assert cookie_mod.read_session_id(request) == "session-abc-123"


@pytest.mark.unit
def test_tampered_cookie_returns_none(isolated_xdg: Path) -> None:
    """A flipped byte in the cookie value fails verification -> None."""
    response = Response()
    cookie_mod.issue_cookie(response, "session-abc-123")
    jar = SimpleCookie()
    jar.load(_set_cookie_lines(response)[0])
    signed_value = jar["ovid2_session"].value

    # Corrupt the first char of the HMAC segment. (The last char may only
    # carry base64 padding bits, which made a last-char flip flaky.)
    head, _, sig = signed_value.rpartition(".")
    tampered = f"{head}.{'A' if sig[0] != 'A' else 'B'}{sig[1:]}"
    assert tampered != signed_value

    request = _make_request(cookies={"ovid2_session": tampered})
    assert cookie_mod.read_session_id(request) is None


@pytest.mark.unit
def test_absent_cookie_returns_none(isolated_xdg: Path) -> None:
    """No cookie present -> read returns None."""
    request = _make_request()
    assert cookie_mod.read_session_id(request) is None


@pytest.mark.unit
def test_cookie_has_httponly_and_samesite_lax(isolated_xdg: Path) -> None:
    """The issued cookie carries HttpOnly and SameSite=Lax."""
    response = Response()
    cookie_mod.issue_cookie(response, "session-xyz")
    line = _set_cookie_lines(response)[0]
    lowered = line.lower()
    assert "httponly" in lowered
    assert "samesite=lax" in lowered


@pytest.mark.unit
def test_cookie_value_carries_no_identity_or_role(isolated_xdg: Path) -> None:
    """The cookie value is the signed session id only — no identity/role."""
    response = Response()
    cookie_mod.issue_cookie(response, "session-opaque-id")
    line = _set_cookie_lines(response)[0]
    # No identity/role tokens leak into the cookie payload.
    assert "role" not in line.lower()
    assert "system_admin" not in line
    assert "email" not in line.lower()


@pytest.mark.unit
def test_secure_flag_set_for_forwarded_https(isolated_xdg: Path) -> None:
    """Secure is set when X-Forwarded-Proto=https and trust is enabled."""
    request = _make_request(
        scheme="http",
        headers=[(b"x-forwarded-proto", b"https")],
    )
    response = Response()
    cookie_mod.issue_cookie(response, "session-secure", request=request)
    line = _set_cookie_lines(response)[0]
    assert "secure" in line.lower()


@pytest.mark.unit
def test_secure_flag_set_for_direct_https(isolated_xdg: Path) -> None:
    """Secure is set when the request scheme itself is https."""
    request = _make_request(scheme="https")
    response = Response()
    cookie_mod.issue_cookie(response, "session-secure", request=request)
    line = _set_cookie_lines(response)[0]
    assert "secure" in line.lower()


@pytest.mark.unit
def test_secure_flag_absent_for_plain_http(isolated_xdg: Path) -> None:
    """Secure is NOT set for a plain http request with no forwarding."""
    request = _make_request(scheme="http")
    response = Response()
    cookie_mod.issue_cookie(response, "session-plain", request=request)
    line = _set_cookie_lines(response)[0]
    assert "secure" not in line.lower()


@pytest.mark.unit
def test_secure_flag_absent_when_no_request(isolated_xdg: Path) -> None:
    """Secure is NOT set when no request is supplied (cannot prove https)."""
    response = Response()
    cookie_mod.issue_cookie(response, "session-no-req")
    line = _set_cookie_lines(response)[0]
    assert "secure" not in line.lower()


@pytest.mark.unit
def test_clear_cookie_deletes(isolated_xdg: Path) -> None:
    """clear_cookie emits a deletion Set-Cookie for the same key."""
    response = Response()
    cookie_mod.clear_cookie(response)
    line = _set_cookie_lines(response)[0]
    assert line.startswith("ovid2_session=")
    lowered = line.lower()
    # A deletion sets an expiry in the past / Max-Age=0.
    assert "expires=" in lowered or "max-age=0" in lowered


@pytest.mark.unit
def test_signing_key_persisted_0600_and_survives_restart(isolated_xdg: Path) -> None:
    """The signing key is persisted 0o600 and reused across cache resets."""
    response = Response()
    cookie_mod.issue_cookie(response, "session-persist")
    jar = SimpleCookie()
    jar.load(_set_cookie_lines(response)[0])
    signed_value = jar["ovid2_session"].value

    secret_path = isolated_xdg / "config" / "ovid2" / "secrets.d" / "auth_session.json"
    assert secret_path.is_file()
    mode = stat.S_IMODE(secret_path.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {mode:04o}"

    # Simulate a process restart: drop the in-process cache; the key is
    # re-read from disk and old cookies still verify.
    cookie_mod.reset_signing_key_cache()
    request = _make_request(cookies={"ovid2_session": signed_value})
    assert cookie_mod.read_session_id(request) == "session-persist"
