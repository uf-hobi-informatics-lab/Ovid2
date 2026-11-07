"""Export the manual as a GitHub wiki (``ovid2 manual wiki``).

A GitHub wiki is a flat git repository of Markdown pages rendered with
GitHub-Flavoured Markdown. This module turns the manual tree into one:

- every page becomes ``<Page-Name>.md``, named from its title
  (``"Connections & addresses"`` → ``Connections-addresses``);
- ``Home.md`` is the manual landing page (tagline, reader paths, chapters);
- ``_Sidebar.md`` is the chapter → page tree; ``_Footer.md`` a
  generated-from-source notice;
- images are copied to ``assets/`` and linked relatively;
- the manual-only syntax is lowered to plain GFM: relative ``.md`` links
  become wiki page links, card grids become bullet lists, definition lists
  become bold-term bullets, and ``> [!WARNING] Title`` callouts become
  GitHub alerts with the title on its own line;
- each page ends with Previous / Next links in reading order.

Code fences and inline code spans are never rewritten. The exporter
depends only on :mod:`ovid2.manual.loader` (YAML) and the standard library,
so it runs from a bare source tree (the public-mirror publish uses it on
the sanitised snapshot).
"""

from __future__ import annotations

import html
import posixpath
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from ovid2.manual.loader import Manual, Page, load_manual

__all__ = ["WikiExport", "export_wiki", "wiki_page_name"]

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_CODE_SPAN_RE = re.compile(r"(`+)(?:(?!\1).)+?\1")
_MD_LINK_RE = re.compile(r"(!?)\[((?:[^\[\]]|\[[^\]]*\])*)\]\(([^)\s]+)\)")
_HTML_ATTR_RE = re.compile(r'\b(href|src)="([^"]*)"')
_CARDS_OPEN_RE = re.compile(r'^\s*<div class="cards">\s*$')
_CARDS_INLINE_RE = re.compile(r'^\s*<div class="cards">(.*)</div>\s*$')
_CARD_RE = re.compile(
    r'<a class="card" href="(?P<href>[^"]*)">\s*(?:<strong>(?P<title>.*?)</strong>)?'
    r"\s*(?:<span>(?P<desc>.*?)</span>)?(?P<rest>.*?)</a>",
    re.S,
)
_CALLOUT_RE = re.compile(r"^>\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]*(.*)$", re.I)
_TAG_RE = re.compile(r"<(/?)(code|strong|em|kbd|b|i)>")
_NAME_STRIP_RE = re.compile(r"[^A-Za-z0-9]+")


@dataclass
class WikiExport:
    """Result of :func:`export_wiki`.

    Attributes:
        pages: Wiki page names written (without ``.md``), in reading order.
        broken: Links / images that could not be resolved.
    """

    pages: list[str] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)


def wiki_page_name(title: str) -> str:
    """Return the wiki file stem for a page title.

    GitHub shows a wiki page's file name, with hyphens rendered as spaces,
    as its title, so the name is the title with every run of
    non-alphanumerics collapsed to one hyphen.

    Args:
        title: Page title from front-matter.

    Returns:
        A file-system- and URL-safe page name (e.g. ``What-is-Ovid2``).
    """
    return _NAME_STRIP_RE.sub("-", title).strip("-") or "Page"


class _Resolver:
    """Map manual-relative hrefs to wiki targets for one manual."""

    def __init__(self, manual: Manual, names: dict[str, str], result: WikiExport) -> None:
        self.manual = manual
        self.names = names
        self.result = result

    def target(self, page: Page, href: str, *, is_image: bool) -> str:
        """Return the wiki href for ``href`` written in ``page``."""
        if not href or _SCHEME_RE.match(href) or href.startswith(("//", "#", "/")):
            return href
        parts = urlsplit(href)
        anchor = f"#{parts.fragment}" if parts.fragment else ""
        target = posixpath.normpath(posixpath.join(page.chapter_slug, parts.path))
        if target.startswith("assets/"):
            if not (self.manual.root / target).is_file():
                self.result.broken.append(f"{page.key}: missing asset {href!r}")
            return target + anchor
        if is_image:
            self.result.broken.append(f"{page.key}: images must live under assets/ ({href!r})")
            return href
        if target.endswith(".md"):
            target = target[: -len(".md")]
        if target.endswith(("/index", "/README")):
            target = target.rsplit("/", 1)[0]
        chapter = self.manual.chapter(target)
        if chapter is not None:
            target = chapter.landing.key
        name = self.names.get(target)
        if name is None:
            self.result.broken.append(f"{page.key}: link to unknown page {href!r}")
            return href
        return name + anchor


def _mask_code_spans(line: str) -> tuple[str, list[str]]:
    spans: list[str] = []

    def keep(match: re.Match[str]) -> str:
        spans.append(match.group(0))
        return f"\x00{len(spans) - 1}\x00"

    return _CODE_SPAN_RE.sub(keep, line), spans


def _unmask(line: str, spans: list[str]) -> str:
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], line)


def _html_to_md(fragment: str) -> str:
    """Lower the few inline HTML tags used in cards to Markdown."""
    out = _TAG_RE.sub(
        lambda m: {"code": "`", "kbd": "`", "strong": "**", "b": "**", "em": "*", "i": "*"}[
            m.group(2)
        ],
        fragment,
    )
    return html.unescape(re.sub(r"\s+", " ", out)).strip()


def _convert_cards(block: str, page: Page, res: _Resolver) -> list[str]:
    items: list[str] = []
    for card in _CARD_RE.finditer(block):
        href = res.target(page, html.unescape(card.group("href")), is_image=False)
        title = _html_to_md(card.group("title") or card.group("rest") or href)
        desc = _html_to_md(card.group("desc") or "")
        items.append(f"- **[{title}]({href})**" + (f" — {desc}" if desc else ""))
    return items


def _rewrite_line(line: str, page: Page, res: _Resolver) -> str:
    masked, spans = _mask_code_spans(line)

    def md_link(match: re.Match[str]) -> str:
        bang, text, href = match.groups()
        return f"{bang}[{text}]({res.target(page, href, is_image=bool(bang))})"

    def html_attr(match: re.Match[str]) -> str:
        attr, value = match.group(1), html.unescape(match.group(2))
        new = res.target(page, value, is_image=attr == "src")
        return f'{attr}="{html.escape(new, quote=True)}"'

    masked = _MD_LINK_RE.sub(md_link, masked)
    masked = _HTML_ATTR_RE.sub(html_attr, masked)
    return _unmask(masked, spans)


def _convert_body(page: Page, res: _Resolver) -> str:
    """Return ``page.body`` lowered to GitHub-Flavoured Markdown."""
    lines = page.body.splitlines()
    out: list[str] = []
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence:
            out.append(line)
            i += 1
            continue
        if _CARDS_OPEN_RE.match(line):
            block: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip() != "</div>":
                block.append(lines[i])
                i += 1
            i += 1  # skip </div>
            out.extend(_convert_cards("\n".join(block), page, res))
            continue
        inline_cards = _CARDS_INLINE_RE.match(line)
        if inline_cards:
            out.extend(_convert_cards(inline_cards.group(1), page, res))
            i += 1
            continue
        callout = _CALLOUT_RE.match(line)
        if callout:
            out.append(f"> [!{callout.group(1).upper()}]")
            if callout.group(2).strip():
                out.append(f"> **{callout.group(2).strip()}**")
                out.append(">")
            i += 1
            continue
        if line.startswith(": ") and out and out[-1].strip() and not out[-1].startswith("- "):
            # Definition list: previous line is the term.
            term = out.pop()
            out.append(f"- **{_rewrite_line(term, page, res).strip()}** — " + line[2:].strip())
            i += 1
            while i < len(lines) and lines[i].startswith("  ") and lines[i].strip():
                out.append(_rewrite_line(lines[i], page, res))
                i += 1
            continue
        out.append(_rewrite_line(line, page, res))
        i += 1
    return "\n".join(out).strip() + "\n"


def _nav_footer(page: Page, manual: Manual, names: dict[str, str]) -> str:
    links: list[str] = []
    if page.prev_key:
        prev = manual.pages[page.prev_key]
        links.append(f"← Previous: [{prev.title}]({names[prev.key]})")
    if page.next_key:
        nxt = manual.pages[page.next_key]
        links.append(f"Next: [{nxt.title}]({names[nxt.key]}) →")
    return "\n---\n\n" + " · ".join(links) + "\n" if links else ""


def _home(manual: Manual, names: dict[str, str]) -> str:
    parts = [f"# {manual.title}", "", manual.tagline.strip(), ""]
    if manual.audiences:
        parts += ["## Start here", ""]
        for aud in manual.audiences.values():
            starts = ", ".join(
                f"[{manual.pages[k].title}]({names[k]})" for k in aud.start if k in manual.pages
            )
            parts.append(f"- **{aud.label}** — {aud.description.strip()} Start with: {starts}.")
        parts.append("")
    parts += ["## Chapters", ""]
    for chapter in manual.chapters:
        parts.append(
            f"{chapter.number}. **[{chapter.title}]({names[chapter.landing.key]})**"
            + (f" — {chapter.summary}" if chapter.summary else "")
        )
    return "\n".join(parts).rstrip() + "\n"


def _sidebar(manual: Manual, names: dict[str, str]) -> str:
    parts = [f"**[{manual.title}](Home)**", ""]
    for chapter in manual.chapters:
        parts.append(f"**{chapter.number:02d} · [{chapter.title}]({names[chapter.landing.key]})**")
        parts.extend(f"- [{p.title}]({names[p.key]})" for p in chapter.pages[1:])
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


_FOOTER = (
    "Generated from `src/ovid2/resources/manual/` by `ovid2 manual wiki` — "
    "edit the source in the repository, not this wiki. The same manual is served "
    "by the Ovid2 web panel at `/manual`.\n"
)


def export_wiki(out: Path, *, manual: Manual | None = None, strict: bool = True) -> WikiExport:
    """Write the manual as a GitHub wiki tree into ``out``.

    Existing ``*.md`` files and the ``assets/`` directory in ``out`` are
    replaced; anything else (e.g. a ``.git`` directory) is left alone, so
    ``out`` can be a wiki clone.

    Args:
        out: Destination directory (created when missing).
        manual: Pre-loaded manual; defaults to :func:`load_manual`.
        strict: Raise when any link or image cannot be resolved.

    Returns:
        The :class:`WikiExport` summary.

    Raises:
        ValueError: ``strict`` and a link / image is broken, or two pages
            map to the same wiki name.
    """
    manual = manual or load_manual()
    result = WikiExport()
    names: dict[str, str] = {}
    seen: dict[str, str] = {}
    for page in manual.ordered_pages():
        name = wiki_page_name(page.title)
        if name in seen or name in {"Home", "_Sidebar", "_Footer"}:
            raise ValueError(
                f"wiki page name {name!r} for {page.key} clashes with {seen.get(name)}"
            )
        seen[name] = page.key
        names[page.key] = name
    res = _Resolver(manual, names, result)

    rendered: dict[str, str] = {}
    for page in manual.ordered_pages():
        lead = f"*{page.summary.strip()}*\n\n" if page.summary.strip() else ""
        body = _convert_body(page, res)
        rendered[names[page.key]] = lead + body + _nav_footer(page, manual, names)
        result.pages.append(names[page.key])
    if strict and result.broken:
        raise ValueError("broken manual links:\n  " + "\n  ".join(result.broken))

    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*.md"):
        stale.unlink()
    if (out / "assets").is_dir():
        shutil.rmtree(out / "assets")
    if manual.asset_dir.is_dir():
        shutil.copytree(manual.asset_dir, out / "assets")
    for name, text in rendered.items():
        (out / f"{name}.md").write_text(text, encoding="utf-8")
    (out / "Home.md").write_text(_home(manual, names), encoding="utf-8")
    (out / "_Sidebar.md").write_text(_sidebar(manual, names), encoding="utf-8")
    (out / "_Footer.md").write_text(_FOOTER, encoding="utf-8")
    return result


if __name__ == "__main__":
    import sys

    target = Path(sys.argv[1] if len(sys.argv) > 1 else "wiki")
    summary = export_wiki(target)
    sys.stdout.write(f"{len(summary.pages)} pages → {target}\n")
