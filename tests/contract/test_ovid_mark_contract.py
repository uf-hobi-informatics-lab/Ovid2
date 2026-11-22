"""Contract: the ``<ovid-mark>`` web component matches its design spec.

Spec: the ovid-mark-spec design note (Claude Design
handoff). Pins the public API — the state list, the CSS variables a
theme may set, the change event, ``flick()`` — the exact geometry, and
the motion rule: only ``transform``, ``opacity``, ``stroke-dashoffset``
and ``fill-opacity`` are animated (no layout or colour keyframes), with
a reduced-motion off switch. Also pins the status driver's state
mapping and that it reuses existing transports (no new endpoint).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_STATIC = Path(__file__).resolve().parents[2] / "src" / "ovid2" / "web" / "static"
_MARK = _STATIC / "ovid_mark.js"
_STATUS = _STATIC / "ovid_mark_status.js"

_STATES = ("idle", "extract", "transform", "load", "running", "sync", "success", "warning", "error")
_VARS = (
    "--ovid-base",
    "--ovid-extract",
    "--ovid-transform",
    "--ovid-load",
    "--ovid-success",
    "--ovid-warning",
    "--ovid-error",
)
_ALLOWED_ANIMATED = {"transform", "opacity", "stroke-dashoffset", "fill-opacity"}


@pytest.mark.contract
def test_mark_declares_every_state() -> None:
    """The component accepts exactly the spec's nine states."""
    js = _MARK.read_text(encoding="utf-8")
    block = js[js.index("var STATES = [") : js.index("];", js.index("var STATES = ["))]
    assert tuple(re.findall(r'"([a-z]+)"', block)) == _STATES


@pytest.mark.contract
def test_mark_public_api() -> None:
    """CSS variables, the change event, flick() and the success timeout exist."""
    js = _MARK.read_text(encoding="utf-8")
    for var in _VARS:
        assert f"var({var}," in js, f"{var} is not consumed by the component"
    assert '"ovid-state-change"' in js
    assert "detail: { from: from, to: to }" in js
    assert "flick()" in js
    assert "4000" in js, "success must auto-return to idle after 4 s"
    assert 'customElements.define("ovid-mark"' in js
    assert 'setAttribute("role", "img")' in js
    assert 'aria-live", "polite"' in js


@pytest.mark.contract
def test_mark_geometry_matches_spec() -> None:
    """The exact viewBox and paths from the handoff."""
    js = _MARK.read_text(encoding="utf-8")
    assert 'VIEWBOX = "0 2 200 160"' in js
    for d in (
        "M100 131.8 C76 80 40 60 10.9 73.9 C10 105 40 134 79.4 129.7",
        "M100 131.8 C124 80 160 60 189.1 73.9 C190 105 160 134 120.6 129.7",
        "M100 104.2 C137.24 75.73 137.24 37.77 100 9.3 C62.76 37.77 62.76 75.73 100 104.2Z",
        "M100 131.8 L100 152",
        "M10.9 73.9 Q100 -55.3 189.1 73.9",
    ):
        assert d in js, d


@pytest.mark.contract
def test_mark_animates_only_compositor_properties() -> None:
    """Keyframes touch only transform / opacity / stroke-dashoffset / fill-opacity."""
    js = _MARK.read_text(encoding="utf-8")
    # Static keyframe blocks authored inline in the CSS string array.
    bodies = re.findall(r"@keyframes [a-z0-9-]+ \{(.*?\})\s*\}?\"", js, flags=re.DOTALL)
    assert bodies, "no keyframes found"
    # Dynamic packet keyframes are built by packetKeyframes(): transform + opacity only.
    builder = js[js.index("function packetKeyframes") : js.index("var CSS = [")]
    bodies.append(builder)
    for body in bodies:
        props = set(re.findall(r"([a-z-]+)\s*:\s*[^;{}]", re.sub(r"\d+(\.\d+)?%", "", body)))
        props.discard("from")
        props.discard("to")
        stray = props - _ALLOWED_ANIMATED
        assert not stray, f"keyframes animate non-compositor properties: {sorted(stray)}"


@pytest.mark.contract
def test_mark_has_reduced_motion_switch() -> None:
    """Under prefers-reduced-motion nothing animates."""
    js = _MARK.read_text(encoding="utf-8")
    assert "@media (prefers-reduced-motion: reduce)" in js
    assert "animation: none !important" in js


@pytest.mark.contract
def test_status_driver_maps_stages_and_reuses_transports() -> None:
    """Stage mapping per spec; polls only the existing jobs list API."""
    js = _STATUS.read_text(encoding="utf-8")
    for stage, state in (
        ("read", "extract"),
        ("format", "transform"),
        ("deduplicate", "transform"),
        ("transform", "transform"),
        ("map", "transform"),
        ("fix", "transform"),
        ("write", "load"),
    ):
        assert re.search(rf'\b{stage}: "{state}"', js), (stage, state)
    urls = set(re.findall(r'"(/api/v1/[^"?]+)', js))
    assert urls == {"/api/v1/jobs"}, urls
    assert "EventSource" not in js and "WebSocket" not in js
    assert "data-poll-runs" in js
    assert "document.hidden" in js
