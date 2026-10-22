"""Contract test: web templates never reference third-party CDN URLs.

Ovid2 runs on air-gapped hospital networks. Every script / stylesheet
/ image the browser needs must be served by the ovid2 web tier itself,
under ``/static/``, ``/branding/``, or a same-origin route.

This test walks every ``.html`` file under ``src/ovid2/web/templates/``
and asserts that every ``src="..."`` and ``href="..."`` value either:

- starts with ``/`` (same-origin absolute path), or
- is a template expression (``{{ ... }}``), or
- is a well-known safe scheme (``mailto:``, ``tel:``, ``data:``,
  fragment ``#``).

The trigger for this test is the introduction of the D3 vendor bundle
under ``/static/vendor/d3.v7.min.js``. Without this guard, a future
"just add unpkg / jsdelivr" regression would silently break air-gapped
installs.
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATES_ROOT = (
    Path(__file__).resolve().parents[2] / "src" / "ovid2" / "web" / "templates"
)

_ATTR_RE = re.compile(r'(?:src|href)\s*=\s*"([^"]*)"', re.IGNORECASE)


def _is_safe_url(url: str) -> bool:
    """Return True when ``url`` cannot reach a third-party host."""
    url = url.strip()
    if not url:
        return True
    # Template expression — the value is server-controlled at render.
    if "{{" in url or "{%" in url:
        return True
    if url.startswith("/"):
        return True
    if url.startswith("#"):
        return True
    if url.startswith(("mailto:", "tel:", "data:")):
        return True
    return False


def test_no_external_urls_in_templates() -> None:
    """Every src/href in a template must be same-origin or template-var."""
    bad: list[tuple[str, str]] = []
    for path in sorted(TEMPLATES_ROOT.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for match in _ATTR_RE.finditer(text):
            url = match.group(1)
            if not _is_safe_url(url):
                rel = path.relative_to(TEMPLATES_ROOT.parents[3])
                bad.append((str(rel), url))
    assert not bad, (
        "Third-party URLs found in web templates. Every asset must be "
        "self-hosted under /static/ or /branding/ (air-gapped install "
        "safety). Offenders:\n  "
        + "\n  ".join(f"{p}: {u}" for p, u in bad)
    )


def test_d3_is_self_hosted() -> None:
    """The base template references the self-hosted D3 bundle."""
    base = TEMPLATES_ROOT / "base.html"
    text = base.read_text(encoding="utf-8")
    assert "/static/vendor/d3.v7.min.js" in text, (
        "base.html must load D3 from /static/vendor/. Do not rely on a "
        "CDN; the panel runs on air-gapped hospital networks."
    )
    # Bundle must actually exist alongside the template.
    vendor = (
        TEMPLATES_ROOT.parent
        / "static"
        / "vendor"
        / "d3.v7.min.js"
    )
    assert vendor.exists(), (
        f"self-hosted D3 bundle missing at {vendor}. Fetch it into place."
    )
    # Sanity — the file starts with a d3 header.
    head = vendor.read_bytes()[:200].decode("utf-8", errors="replace")
    assert "d3js.org" in head or "d3" in head.lower(), (
        f"vendor/d3.v7.min.js at {vendor} does not look like a real D3 "
        f"bundle. First 200 bytes: {head!r}"
    )
