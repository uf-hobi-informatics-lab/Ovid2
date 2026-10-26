"""Contract test: every display widget honours the widget contract.

Widgets under ``src/ovid2/web/static/display/widgets/*.js`` are the
extensible seam of the live-canvas display bus (see
``src/ovid2/web/static/display/canvas.js``). The bus calls a widget's
lifecycle methods; missing any of them silently breaks the surface.

Contract: each widget file must export a default object literal that
contains — at minimum — the keys ``id``, ``mount``, ``update``,
``tick``, ``unmount``. Optional keys (``slot``, ``resize``) are not
enforced here.

We check statically with regex — no JS runtime dependency in the
Python test suite. That is fine because the widget files are hand-
authored and short; if a future refactor introduces dynamically
generated widget shapes, we can add a Node-based runtime check.
"""

from __future__ import annotations

import re
from pathlib import Path

WIDGETS_ROOT = (
    Path(__file__).resolve().parents[2] / "src" / "ovid2" / "web" / "static" / "display" / "widgets"
)

REQUIRED_KEYS = ("id", "mount", "update", "tick", "unmount")

_EXPORT_RE = re.compile(
    r"export\s+default\s*(\{.*?\});?\s*$",
    re.DOTALL,
)


def _widget_files() -> list[Path]:
    return sorted(WIDGETS_ROOT.glob("*.js"))


def test_widgets_directory_exists() -> None:
    assert WIDGETS_ROOT.is_dir(), f"expected widgets directory at {WIDGETS_ROOT}"
    assert _widget_files(), "no widget modules found; the display bus needs at least one."


def test_every_widget_has_required_keys() -> None:
    """Each widget file exports a default object with the lifecycle keys."""
    missing: list[tuple[str, list[str]]] = []
    for path in _widget_files():
        source = path.read_text(encoding="utf-8")
        m = _EXPORT_RE.search(source)
        assert m, f"{path.name}: no `export default {{ ... }};` block found"
        block = m.group(1)
        # Look for `key:` at the start of a line (allow whitespace),
        # since object-literal keys are unquoted identifiers here.
        gone = [k for k in REQUIRED_KEYS if not re.search(rf"\b{k}\s*[:(]", block)]
        if gone:
            missing.append((path.name, gone))
    assert not missing, "widget files missing required lifecycle keys:\n  " + "\n  ".join(
        f"{n}: {m}" for n, m in missing
    )


def test_widget_ids_are_unique() -> None:
    """No two widget files may declare the same ``id: "..."``."""
    _id_re = re.compile(r'\bid\s*:\s*["\']([a-z0-9_]+)["\']')
    seen: dict[str, str] = {}
    dupes: list[tuple[str, str, str]] = []
    for path in _widget_files():
        source = path.read_text(encoding="utf-8")
        m = _id_re.search(source)
        assert m, f"{path.name}: could not extract widget id"
        wid = m.group(1)
        if wid in seen:
            dupes.append((wid, seen[wid], path.name))
        else:
            seen[wid] = path.name
    assert not dupes, "duplicate widget ids:\n  " + "\n  ".join(
        f"{i}: {a} vs {b}" for i, a, b in dupes
    )


def test_canvas_and_bus_modules_present() -> None:
    """The canvas + bus + widget modules exist and stay self-consistent.

    These modules are the agent-driveable display seam: a future MCP
    tool can dynamically import ``canvas.js`` + a widget module to push
    a live widget into a running page. They ship regardless of whether
    any given page mounts them today (the run page is now a full-width
    terminal console and no longer mounts the metrics canvas), so the
    contract checks the modules against each other rather than against
    a specific bootstrap file.
    """
    base = WIDGETS_ROOT.parent
    for name in ("canvas.js", "bus.js"):
        assert (base / name).exists(), f"display module missing: {base / name}"
    # canvas.js is the widget host — it must wire up the bus.
    canvas_text = (base / "canvas.js").read_text(encoding="utf-8")
    assert "bus.js" in canvas_text, "canvas.js must import the display bus (./bus.js)"
    assert "mountCanvas" in canvas_text, (
        "canvas.js must export the mountCanvas entry point so an agent "
        "or bootstrap can host widgets."
    )
    # Every widget on disk must satisfy the lifecycle contract (checked
    # in test_every_widget_has_required_keys) — here we just assert the
    # folder is non-empty so the seam has at least one reference widget.
    assert _widget_files(), (
        "no widget modules found; the display seam needs at least one reference widget."
    )
