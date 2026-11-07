"""Unit tests for the manual loader, renderer, search index and static builder."""

from __future__ import annotations

from pathlib import Path

import pytest

from ovid2.manual import ManualError, load_manual
from ovid2.manual.layout import SEARCH_SURFACE, STATIC_SURFACE, WEB_SURFACE
from ovid2.manual.loader import orphan_pages, split_front_matter
from ovid2.manual.render import check_links, render_page, slugify
from ovid2.manual.search import build_search_index

pytestmark = pytest.mark.unit


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def mini(tmp_path: Path) -> Path:
    _write(
        tmp_path,
        "manual.yaml",
        """
title: Mini
audiences:
  user: {label: Operator, description: Runs things., start: [a-one/index]}
chapters:
  - {slug: a-one, title: One, pages: [index, second]}
  - {slug: b-two, title: Two, pages: [index]}
""",
    )
    _write(
        tmp_path,
        "a-one/index.md",
        """---
title: First
summary: The first page.
audience: [user]
---

Intro with a [link](second.md#details) and a [chapter](../b-two/index.md).

## Details

> [!WARNING] Mind the gap
> Body text.

Term
: Definition.

![diagram](../assets/d.svg)

<div class="cards"><a class="card" href="second.md">x</a></div>

```bash
echo hi
```

| a | b |
| - | - |
| 1 | 2 |
""",
    )
    _write(tmp_path, "a-one/second.md", "---\ntitle: Second\n---\n\n## Details\n\ntext\n")
    _write(tmp_path, "b-two/index.md", "---\ntitle: Third\n---\n\nSee [missing](nope.md).\n")
    _write(tmp_path, "assets/d.svg", "<svg xmlns='http://www.w3.org/2000/svg'/>")
    return tmp_path


def test_split_front_matter() -> None:
    meta, body = split_front_matter("---\ntitle: X\n---\n\nbody\n")
    assert meta == {"title": "X"} and body == "body\n"
    assert split_front_matter("plain") == ({}, "plain")
    with pytest.raises(ManualError):
        split_front_matter("---\ntitle: X\n")


def test_load_builds_tree_and_reading_order(mini: Path) -> None:
    manual = load_manual(mini)
    assert [c.slug for c in manual.chapters] == ["a-one", "b-two"]
    first = manual.pages["a-one/index"]
    assert first.next_key == "a-one/second"
    assert manual.pages["b-two/index"].prev_key == "a-one/second"
    assert manual.chapters[0].number == 1 and manual.chapters[0].audience == ("user",)
    assert orphan_pages(manual) == []
    _write(mini, "a-one/stray.md", "---\ntitle: S\n---\n")
    assert orphan_pages(load_manual(mini)) == ["a-one/stray.md"]


def test_load_rejects_unknown_audience(mini: Path) -> None:
    _write(mini, "a-one/second.md", "---\ntitle: S\naudience: [martian]\n---\n")
    with pytest.raises(ManualError, match="unknown audience"):
        load_manual(mini)


def test_render_rewrites_links_and_decorates(mini: Path) -> None:
    manual = load_manual(mini)
    out = render_page(manual.pages["a-one/index"], manual, WEB_SURFACE)
    assert 'href="/manual/a-one/second#details"' in out.html
    assert 'href="/manual/b-two/index"' in out.html  # chapter link → landing page
    assert 'src="/manual/assets/d.svg"' in out.html
    assert 'class="card" href="/manual/a-one/second"' in out.html  # raw HTML rewritten
    assert 'class="callout callout-warning"' in out.html and "Mind the gap" in out.html
    assert "[!WARNING]" not in out.html
    assert 'id="details"' in out.html and 'class="heading-link"' in out.html
    assert '<dt id="term">' in out.html
    assert 'class="table-wrap"' in out.html
    assert 'data-lang="bash"' in out.html and "tok-" in out.html
    assert [t.anchor for t in out.toc] == ["details"]


def test_static_surface_uses_relative_urls(mini: Path) -> None:
    manual = load_manual(mini)
    page = manual.pages["a-one/index"]
    out = render_page(page, manual, STATIC_SURFACE.for_page(page))
    assert 'href="../a-one/second.html#details"' in out.html
    assert 'src="../assets/d.svg"' in out.html


def test_check_links_reports_dangling_targets(mini: Path) -> None:
    problems = check_links(load_manual(mini))
    assert problems == ["b-two/index: link to unknown page 'nope.md'"]
    _write(mini, "b-two/index.md", "---\ntitle: T\n---\n\n[x](../a-one/second.md#nowhere)\n")
    assert check_links(load_manual(mini)) == [
        "b-two/index: anchor #nowhere not found on a-one/second"
    ]


def test_search_index_has_sections(mini: Path) -> None:
    index = build_search_index(load_manual(mini), SEARCH_SURFACE)
    urls = {d["u"] for d in index["docs"]}
    assert "a-one/index" in urls and "a-one/index#details" in urls
    intro = next(d for d in index["docs"] if d["u"] == "a-one/index")
    assert intro["x"].startswith("The first page.")


def test_slugify_matches_github() -> None:
    assert slugify("Overwrite & batches") == "overwrite--batches"
    assert slugify("`ovid2 etl`") == "ovid2-etl"


def test_static_build(tmp_path: Path) -> None:
    from ovid2.manual.static_site import build_static_site

    out = tmp_path / "site"
    count = build_static_site(out)
    manual = load_manual()
    assert count == len(manual.pages) + 1
    for rel in ("index.html", "search.json", "404.html", ".nojekyll", "static/manual.css"):
        assert (out / rel).is_file(), rel
    html = (out / "02-concepts" / "storage.html").read_text(encoding="utf-8")
    assert 'href="../static/manual.css"' in html
    assert "http://" not in html.split("<main")[0]  # no external assets in <head>


def test_wiki_export(mini: Path, tmp_path: Path) -> None:
    from ovid2.manual.wiki import export_wiki, wiki_page_name

    (mini / "b-two/index.md").write_text(
        "---\ntitle: Third\n---\n\nBack to [first](../a-one/index.md#details).\n", encoding="utf-8"
    )
    out = tmp_path / "wiki"
    (out / ".git").mkdir(parents=True)
    (out / "Stale.md").write_text("old", encoding="utf-8")
    result = export_wiki(out, manual=load_manual(mini))

    assert result.pages == ["First", "Second", "Third"]
    assert not (out / "Stale.md").exists() and (out / ".git").is_dir()
    assert {"Home.md", "_Sidebar.md", "_Footer.md"} <= {p.name for p in out.iterdir()}
    assert (out / "assets" / "d.svg").is_file()
    first = (out / "First.md").read_text(encoding="utf-8")
    assert "[link](Second#details)" in first
    assert "[chapter](Third)" in first
    assert "![diagram](assets/d.svg)" in first
    assert "> [!WARNING]\n> **Mind the gap**" in first
    assert "- **Term** — Definition." in first
    assert "- **[x](Second)**" in first and 'class="card' not in first
    assert "```bash\necho hi\n```" in first
    assert "Next: [Second](Second)" in first
    assert "[first](First#details)" in (out / "Third.md").read_text(encoding="utf-8")
    assert "[One](First)" in (out / "Home.md").read_text(encoding="utf-8")
    assert wiki_page_name("Connections & addresses") == "Connections-addresses"


def test_wiki_export_strict_rejects_broken_links(mini: Path, tmp_path: Path) -> None:
    from ovid2.manual.wiki import export_wiki

    with pytest.raises(ValueError, match=r"nope\.md"):
        export_wiki(tmp_path / "wiki", manual=load_manual(mini))


def test_wiki_export_real_manual(tmp_path: Path) -> None:
    from ovid2.manual.wiki import export_wiki

    result = export_wiki(tmp_path / "wiki")
    assert not result.broken
    assert len(result.pages) == len(load_manual().pages)
