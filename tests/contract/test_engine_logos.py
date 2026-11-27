"""Contract: every engine type and storage type has a logo mark.

The Atlas pipeline component draws the *material* (engine type) and the
input / output (storage type) with a logo from ``static/logos.js``. A new
engine in ``config/engines.py::_KNOWN_TYPES`` or a new driver in
``drivers/registry.py::DRIVERS`` needs a mark there too (and
``logoForEngine`` must know the engine type).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

_LOGOS_JS = Path(__file__).resolve().parents[2] / "src" / "ovid2" / "web" / "static" / "logos.js"


def _logo_keys(text: str) -> set[str]:
    block = text[text.index("export const LOGOS = {") : text.index("\n};")]
    return set(re.findall(r"^\s{2}(\w+):\s*$", block, re.M))


def test_every_engine_type_has_a_logo() -> None:
    """Each registered engine type has a mark that ``logoForEngine`` returns."""
    from ovid2.config.engines import _KNOWN_TYPES

    text = _LOGOS_JS.read_text(encoding="utf-8")
    keys = _logo_keys(text)
    fn = text[text.index("export function logoForEngine") :]
    fn = fn[: fn.index("\n}")]
    for engine_type in _KNOWN_TYPES:
        assert engine_type in keys, f"static/logos.js has no logo for engine type {engine_type!r}"
        assert f'"{engine_type}"' in fn, f"logoForEngine does not map {engine_type!r}"


def test_every_storage_type_has_a_logo() -> None:
    """Each registered driver type has a storage mark."""
    from ovid2.drivers.registry import DRIVERS

    keys = _logo_keys(_LOGOS_JS.read_text(encoding="utf-8"))
    missing = sorted(set(DRIVERS) - keys)
    assert not missing, f"static/logos.js has no logo for storage type(s) {missing}"
