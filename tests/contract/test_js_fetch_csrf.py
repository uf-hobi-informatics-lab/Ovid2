"""Contract: background ``fetch`` mutations to HTML routes send the CSRF token.

Every mutating HTML route depends on ``require_csrf``
(``test_html_mutations_guarded.py``). A ``<form>`` carries the token as a
hidden field, but a script that intercepts the submit and POSTs with
``fetch`` does **not** — unless it sends the ``X-CSRF-Token`` header. When
it forgets, the server answers 403 and the action silently does nothing.
That is exactly how the run page's ■ Stop button stopped cancelling jobs
(``display/terminal/mount.js::requestStop`` sent no token).

The scan: for every object literal in ``web/static/**/*.js`` that declares
``method: "POST" | "PUT" | "PATCH" | "DELETE"``, the literal must either

* send the token — its text mentions ``X-CSRF-Token`` or the
  ``fetchPostHeaders(`` helper, or
* be the options of a call whose URL argument targets ``/api/v1`` (the JSON
  API is not CSRF-checked: cross-site forms cannot send JSON), either as a
  literal or as an expression listed in :data:`_API_URL_EXPRESSIONS`.

ADR: the adr-web-auth-rbac design note.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

#: ``(file name, URL-argument expression)`` pairs whose URL is known to be an
#: ``/api/v1`` path even though the call site passes a variable. Each needs
#: the reason in a comment; keep it short.
_API_URL_EXPRESSIONS: frozenset[tuple[str, str]] = frozenset(
    {
        # SESSIONS_API = "/api/v1/llm/sessions"
        ("llm_panel.js", 'sessionUrl(sid) + "/messages/" + m.seq + "/meta"'),
        ("llm_panel.js", "sessionUrl(r.session_id)"),
        # Agent-tool proposals: paths come from the /api/v1 tool catalogue.
        ("llm_panel.js", "p.path"),
        # PREFS_URL = "/api/v1/me/appearance"
        ("theme.js", "PREFS_URL"),
    }
)

_METHOD_RE = re.compile(r"""method\s*:\s*["'](POST|PUT|PATCH|DELETE)["']""", re.IGNORECASE)
_CSRF_MARKERS: tuple[str, ...] = ("X-CSRF-Token", "fetchPostHeaders(")
#: ``someFetch(<url>, `` immediately before the options object.
_CALL_RE = re.compile(r"\b\w*[Ff]etch\(\s*(?P<url>.+?)\s*,\s*$", re.DOTALL)


def _enclosing_object(text: str, pos: int) -> tuple[int, int]:
    """Return ``(start, end)`` of the ``{ … }`` literal enclosing ``pos``."""
    depth = 0
    start = pos
    while start > 0:
        start -= 1
        ch = text[start]
        if ch == "}":
            depth += 1
        elif ch == "{":
            if depth == 0:
                break
            depth -= 1
    depth = 0
    end = pos
    while end < len(text):
        ch = text[end]
        if ch == "{":
            depth += 1
        elif ch == "}":
            if depth == 0:
                break
            depth -= 1
        end += 1
    return start, end + 1


def _violations(name: str, text: str) -> list[int]:
    """Return line numbers of mutating fetch options that skip CSRF."""
    bad: list[int] = []
    for match in _METHOD_RE.finditer(text):
        start, end = _enclosing_object(text, match.start())
        literal = text[start:end]
        if any(marker in literal for marker in _CSRF_MARKERS):
            continue
        call = _CALL_RE.search(text[max(0, start - 300) : start])
        url = call.group("url").strip() if call else ""
        # Only the last argument-ish expression: drop anything before a
        # nested ``fetch(`` that the lazy regex may have swallowed.
        url = url.rsplit("fetch(", 1)[-1].strip()
        if "/api/v1" in url or (name, url) in _API_URL_EXPRESSIONS:
            continue
        bad.append(text[: match.start()].count("\n") + 1)
    return bad


@pytest.mark.contract
def test_mutating_fetches_send_csrf(src_root: Path) -> None:
    """Every non-API mutating ``fetch`` in the web static JS sends the token."""
    static = src_root / "web" / "static"
    scanned = 0
    offenders: list[str] = []
    for path in sorted(static.rglob("*.js")):
        text = path.read_text(encoding="utf-8")
        scanned += len(_METHOD_RE.findall(text))
        offenders += [
            f"{path.relative_to(src_root)}:{line}" for line in _violations(path.name, text)
        ]
    assert scanned >= 5, "suspiciously few mutating fetch calls found"
    assert not offenders, (
        "Mutating fetch() to an HTML route must send the X-CSRF-Token header "
        "(read it from <body hx-headers>), or target /api/v1:\n" + "\n".join(offenders)
    )


@pytest.mark.contract
def test_detector_catches_a_tokenless_fetch() -> None:
    """Negative / positive controls for the scan."""
    bad = 'fetch("/jobs/" + id + "/cancel", {\n  method: "POST",\n  headers: {}\n});'
    assert _violations("x.js", bad) == [2]
    good = 'fetch("/jobs/1/cancel", { method: "POST", headers: { "X-CSRF-Token": t } });'
    assert not _violations("x.js", good)
    api = 'fetch("/api/v1/jobs/1/cancel", { method: "POST" });'
    assert not _violations("x.js", api)
    helper = 'const init = { method: "POST", headers: fetchPostHeaders(form) };'
    assert not _violations("x.js", helper)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
