"""Contract: the Ovid2 Manual is complete, cross-linked and current.

Pins four properties of ``src/ovid2/resources/manual/``:

1. ``manual.yaml`` and every page load (front-matter, audiences, start pages).
2. Every internal link, section anchor and asset resolves; no page exists
   that ``manual.yaml`` does not list (no orphans).
3. The generated *Reference* chapter matches what
   ``ovid2 manual gen-reference`` produces from the live registries.
4. The manual is site-neutral — no line trips the public mirror's
   leak-gate deny patterns (the manual is published).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from ovid2.manual import load_manual
from ovid2.manual.loader import orphan_pages
from ovid2.manual.render import check_links

pytestmark = pytest.mark.contract

_REPO = Path(__file__).resolve().parents[2]
_MANIFEST = _REPO / "scripts" / "public" / "manifest.toml"


def test_manual_loads() -> None:
    manual = load_manual()
    assert len(manual.chapters) >= 10
    assert all(page.title for page in manual.pages.values())
    stubs = [k for k, p in manual.pages.items() if p.body.strip() in {"", "TODO"}]
    assert not stubs, f"stub pages: {stubs}"


def test_manual_links_and_anchors_resolve() -> None:
    problems = check_links(load_manual())
    assert not problems, "broken manual links:\n" + "\n".join(problems)


def test_manual_has_no_orphan_pages() -> None:
    orphans = orphan_pages(load_manual())
    assert not orphans, f"pages missing from manual.yaml: {orphans}"


def test_manual_reference_is_fresh() -> None:
    pytest.importorskip("fastapi")
    from ovid2.manual.reference import write_reference

    stale = write_reference(check=True)
    assert not stale, (
        "generated reference pages are stale — run `ovid2 manual gen-reference`:\n"
        + "\n".join(stale)
    )


def test_manual_is_site_neutral() -> None:
    if not _MANIFEST.is_file():
        pytest.skip("public-mirror manifest not present")
    gate = tomllib.loads(_MANIFEST.read_text(encoding="utf-8"))["leak_gate"]
    deny = [re.compile(p, re.IGNORECASE) for p in gate["deny"]]
    deny += [
        re.compile(rf"\b{re.escape(name)}\b")
        for name in gate.get("deny_pipeline_imports", [])
        if len(name) > 3
    ]
    hits: list[str] = []
    root = load_manual().root
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in {".md", ".yaml", ".svg"}:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for rx in deny:
                if rx.search(line):
                    hits.append(f"{path.relative_to(root)}:{lineno}: {rx.pattern}")
    assert not hits, "manual contains site-specific content:\n" + "\n".join(hits)
