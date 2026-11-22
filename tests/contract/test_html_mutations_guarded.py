"""Contract: every mutating HTML route carries CSRF and an authorization gate.

Discovered from the live FastAPI app (not a hand-kept list): every
non-``/api/v1`` route answering ``POST`` / ``PUT`` / ``PATCH`` / ``DELETE``
must

* depend on :func:`ovid2.web.auth.csrf.require_csrf` (route, router or
  parameter dependency), and
* depend on ``require_capability(...)`` / ``require_role(...)`` — unless it
  is one of the explicitly listed **pre-auth** routes (sign-in), which still
  need CSRF.

Also, every ``<form method="post">`` in ``templates/`` must render the
``csrf_token`` hidden field, so a token-checked route can actually be
submitted from the page.

ADR: the adr-web-auth-rbac design note.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

_MUTATING: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Routes reachable before sign-in. They need CSRF (login CSRF) but cannot
#: require a capability. Keep this list minimal and explicit.
_PRE_AUTH_PATHS: frozenset[str] = frozenset({"/login/breakglass"})


def _dependency_names(route: Any, inherited: Iterable[Any]) -> set[str]:
    """Return the qualified names of every dependency callable of ``route``."""
    names: set[str] = set()
    for dep in [*route.dependencies, *inherited]:
        names.add(getattr(dep.dependency, "__qualname__", repr(dep.dependency)))

    def _walk(dependant: Any) -> None:
        for sub in dependant.dependencies:
            names.add(getattr(sub.call, "__qualname__", repr(sub.call)))
            _walk(sub)

    _walk(route.dependant)
    return names


def _mutating_html_routes(app: Any) -> list[tuple[Any, set[str]]]:
    from tests.contract._routes import iter_api_routes

    out: list[tuple[Any, set[str]]] = []
    for route, inherited in iter_api_routes(app):
        if route.path.startswith("/api/v1/"):
            continue
        if not (route.methods & _MUTATING):
            continue
        out.append((route, _dependency_names(route, inherited)))
    return out


def _violations(app: Any) -> list[str]:
    problems: list[str] = []
    for route, names in _mutating_html_routes(app):
        label = f"{','.join(sorted(route.methods & _MUTATING))} {route.path}"
        if not any(n.endswith("require_csrf") for n in names):
            problems.append(f"{label}: missing Depends(require_csrf)")
        if route.path in _PRE_AUTH_PATHS:
            continue
        if not any("require_capability" in n or "require_role" in n for n in names):
            problems.append(f"{label}: missing require_capability(...) / require_role(...)")
    return problems


@pytest.mark.contract
def test_every_mutating_html_route_is_guarded() -> None:
    """CSRF + capability on every mutating HTML route (pre-auth: CSRF only)."""
    from ovid2.web.app import create_app

    app = create_app()
    assert len(_mutating_html_routes(app)) >= 30, "suspiciously few HTML mutation routes"
    problems = _violations(app)
    assert not problems, (
        "Mutating HTML routes must depend on require_csrf and on "
        "require_capability/require_role:\n" + "\n".join(problems)
    )


@pytest.mark.contract
def test_guard_detector_catches_an_unguarded_route() -> None:
    """Negative control: an unguarded POST is reported."""
    from fastapi import FastAPI

    app = FastAPI()

    @app.post("/widgets")
    def widgets_post() -> None:  # pragma: no cover - never called
        """Unguarded on purpose."""

    problems = _violations(app)
    assert any("require_csrf" in p for p in problems), problems
    assert any("require_capability" in p for p in problems), problems


_FORM_RE = re.compile(r"<form\b[^>]*>", re.IGNORECASE | re.DOTALL)
_POST_RE = re.compile(r"method\s*=\s*[\"']?post", re.IGNORECASE)


def _forms_missing_token(text: str) -> list[int]:
    """Return line numbers of POST forms whose body lacks ``csrf_token``."""
    missing: list[int] = []
    for match in _FORM_RE.finditer(text):
        if not _POST_RE.search(match.group(0)):
            continue
        end = text.find("</form>", match.end())
        body = text[match.end() : end if end != -1 else len(text)]
        if "csrf_token" not in body:
            missing.append(text[: match.start()].count("\n") + 1)
    return missing


@pytest.mark.contract
def test_every_post_form_renders_the_csrf_field(src_root: Path) -> None:
    """Every ``<form method="post">`` template includes the CSRF hidden field."""
    templates = src_root / "web" / "templates"
    offenders = [
        f"{path.relative_to(src_root)}:{line}"
        for path in sorted(templates.rglob("*.html"))
        for line in _forms_missing_token(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "POST forms must include "
        '<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">:\n'
        + "\n".join(offenders)
    )


@pytest.mark.contract
def test_form_detector_catches_a_tokenless_form() -> None:
    """Negative control for the template scan."""
    assert _forms_missing_token('<form method="post" action="/x"><button></button></form>')
    assert not _forms_missing_token(
        '<form method="post"><input name="csrf_token" value="{{ csrf_token() }}"></form>'
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
