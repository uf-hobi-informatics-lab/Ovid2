"""Markdown → HTML for manual pages.

One :class:`MarkdownIt` pipeline (CommonMark + tables + strikethrough +
definition lists + raw HTML — page sources are trusted repository files)
with four manual-specific passes over the token stream:

1. **Heading anchors.** Every ``h2``–``h4`` gets a GitHub-style slug id
   and a hover permalink; ``h2`` / ``h3`` feed the page table of contents.
2. **Callouts.** A blockquote whose first line is ``[!NOTE]``,
   ``[!TIP]``, ``[!IMPORTANT]``, ``[!WARNING]`` or ``[!CAUTION]`` (the
   GitHub syntax, so sources still read well on a code host) becomes a
   styled ``<div class="callout callout-note">``.
3. **Cross-links.** Links are written *relative to the source file*
   (``../02-concepts/dataspace.md#paths``) so they also work when browsing
   the repository. They are rewritten through a :class:`LinkResolver` to
   the delivery surface's URL scheme (``/manual/02-concepts/dataspace``
   for the web panel, ``../02-concepts/dataspace.html`` for the static
   site). Images under ``assets/`` are rewritten the same way. Every
   internal target is recorded so :func:`check_links` can fail the build
   on a dangling link or anchor.
4. **Wide tables** are wrapped in a scroll container.

Fenced code is highlighted server-side by Pygments into ``tok-*`` classes
styled by ``manual.css`` with the panel's theme tokens.
"""

from __future__ import annotations

import html
import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.deflist import deflist_plugin
from pygments import highlight as pyg_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

from ovid2.manual.loader import Manual, Page

__all__ = [
    "LinkResolver",
    "RenderedPage",
    "TocEntry",
    "check_links",
    "render_markdown",
    "render_page",
    "slugify",
]

_CALLOUT_RE = re.compile(r"^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]*(.*)$", re.IGNORECASE)
_CALLOUT_LABELS = {
    "note": "Note",
    "tip": "Tip",
    "important": "Important",
    "warning": "Warning",
    "caution": "Caution",
}
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_SLUG_STRIP_RE = re.compile(r"[^\w\- ]+", re.UNICODE)
_HTML_ATTR_RE = re.compile(r'\b(href|src)="([^"]*)"')
_LANG_ALIASES = {"sh": "bash", "shell": "bash", "zsh": "bash", "console": "bash", "yml": "yaml"}


class LinkResolver(Protocol):
    """Maps manual page keys and asset paths to surface-specific URLs."""

    def page_url(self, key: str, anchor: str = "") -> str:
        """Return the URL of page ``key`` (``"<chapter>/<page>"``)."""
        ...

    def asset_url(self, relpath: str) -> str:
        """Return the URL of asset ``relpath`` (relative to ``assets/``)."""
        ...


@dataclass(frozen=True)
class TocEntry:
    """One table-of-contents line."""

    level: int
    anchor: str
    text: str


@dataclass(frozen=True)
class Section:
    """A searchable slice of a page (intro text or one ``h2``/``h3`` section)."""

    anchor: str
    heading: str
    text: str


@dataclass
class RenderedPage:
    """Result of rendering one page.

    Attributes:
        html: The article body HTML.
        toc: ``h2`` / ``h3`` entries in document order.
        anchors: Every heading id on the page.
        page_links: Internal page targets as ``(key, anchor)`` pairs.
        broken: Human-readable descriptions of unresolvable links.
        sections: Plain-text sections for the search index.
    """

    html: str
    toc: list[TocEntry] = field(default_factory=list)
    anchors: set[str] = field(default_factory=set)
    page_links: list[tuple[str, str]] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)


def slugify(text: str) -> str:
    """Return a GitHub-compatible heading slug for ``text``."""
    slug = _SLUG_STRIP_RE.sub("", text.strip().lower())
    return re.sub(r"\s", "-", slug)


def _highlight(code: str, lang: str, _attrs: str) -> str:
    """Pygments highlighter hook for markdown-it fences."""
    name = _LANG_ALIASES.get(lang.lower(), lang.lower()) if lang else ""
    label = html.escape(lang or "text")
    try:
        lexer = get_lexer_by_name(name) if name else None
    except ClassNotFound:
        lexer = None
    body = (
        pyg_highlight(code, lexer, HtmlFormatter(nowrap=True, classprefix="tok-"))
        if lexer is not None
        else html.escape(code)
    )
    return (
        f'<pre class="code" data-lang="{label}"><code class="language-{label}">{body}</code></pre>'
    )


def _build_md() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": True, "linkify": False, "typographer": True})
    md.enable(["table", "strikethrough", "replacements", "smartquotes"])
    md.use(deflist_plugin)
    md.options["highlight"] = _highlight

    def table_open(_self: Any, tokens: list[Token], idx: int, options: Any, env: Any) -> str:
        return '<div class="table-wrap"><table>\n'

    def table_close(_self: Any, tokens: list[Token], idx: int, options: Any, env: Any) -> str:
        return "</table></div>\n"

    md.add_render_rule("table_open", table_open)
    md.add_render_rule("table_close", table_close)
    return md


_MD = _build_md()


def _inline_text(token: Token) -> str:
    """Return the plain text of an ``inline`` token (code spans included)."""
    if not token.children:
        return token.content
    parts: list[str] = []
    for child in token.children:
        if child.type in {"text", "code_inline"}:
            parts.append(child.content)
        elif child.type in {"softbreak", "hardbreak"}:
            parts.append(" ")
    return "".join(parts)


def _apply_callouts(tokens: list[Token]) -> None:
    """Convert ``> [!NOTE]`` blockquotes into callout ``<div>`` blocks."""
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if (
            tok.type == "blockquote_open"
            and idx + 2 < len(tokens)
            and tokens[idx + 1].type == "paragraph_open"
            and tokens[idx + 2].type == "inline"
        ):
            inline = tokens[idx + 2]
            first_line, _, rest = inline.content.partition("\n")
            match = _CALLOUT_RE.match(first_line.strip())
            if match:
                kind = match.group(1).lower()
                title = match.group(2).strip() or _CALLOUT_LABELS[kind]
                tok.tag = "div"
                tok.attrSet("class", f"callout callout-{kind}")
                tok.attrSet("role", "note")
                depth = 0
                for close_idx in range(idx, len(tokens)):
                    t = tokens[close_idx]
                    if t.type == "blockquote_open":
                        depth += 1
                    elif t.type == "blockquote_close":
                        depth -= 1
                        if depth == 0:
                            t.tag = "div"
                            break
                # Strip the marker line from the inline children.
                children = inline.children or []
                cut = 0
                while cut < len(children) and children[cut].type not in {"softbreak", "hardbreak"}:
                    cut += 1
                inline.children = children[cut + 1 :] if cut < len(children) else []
                inline.content = rest
                title_tok = Token("html_block", "", 0)
                title_tok.content = f'<p class="callout-title">{_md_inline_html(title)}</p>\n'
                tokens.insert(idx + 1, title_tok)
                if not inline.children:
                    # Marker-only first paragraph: drop the empty <p></p>.
                    del tokens[idx + 2 : idx + 5]
        idx += 1


def _md_inline_html(text: str) -> str:
    return str(_MD.renderInline(text))


def _resolve_target(
    page: Page,
    href: str,
    manual: Manual,
    resolver: LinkResolver,
    result: RenderedPage,
    *,
    is_image: bool,
) -> str:
    """Rewrite a relative ``href`` and record it for link checking."""
    if not href or _SCHEME_RE.match(href) or href.startswith("//"):
        return href
    parts = urlsplit(href)
    anchor = parts.fragment
    if not parts.path:
        result.page_links.append((page.key, anchor))
        return f"#{anchor}" if anchor else href
    if parts.path.startswith("/"):
        return href  # absolute site path — the author knows best
    target = posixpath.normpath(posixpath.join(page.chapter_slug, parts.path))
    if target.startswith("assets/"):
        if not (manual.root / target).is_file():
            result.broken.append(f"{page.key}: missing asset {href!r}")
        return resolver.asset_url(target[len("assets/") :])
    if is_image:
        result.broken.append(f"{page.key}: images must live under assets/ ({href!r})")
        return href
    if target.endswith(".md"):
        target = target[: -len(".md")]
    if target.endswith("/index") or target.endswith("/README"):
        target = target.rsplit("/", 1)[0]
    chapter = manual.chapter(target)
    if chapter is not None:
        target = chapter.landing.key
    if target not in manual.pages:
        result.broken.append(f"{page.key}: link to unknown page {href!r}")
        return href
    result.page_links.append((target, anchor))
    return resolver.page_url(target, anchor)


def _rewrite_html(
    content: str, page: Page, manual: Manual, resolver: LinkResolver, result: RenderedPage
) -> str:
    """Rewrite relative ``href`` / ``src`` attributes inside raw HTML."""

    def sub(match: re.Match[str]) -> str:
        attr, value = match.group(1), html.unescape(match.group(2))
        new = _resolve_target(page, value, manual, resolver, result, is_image=attr == "src")
        return f'{attr}="{html.escape(new, quote=True)}"'

    return _HTML_ATTR_RE.sub(sub, content)


def render_page(page: Page, manual: Manual, resolver: LinkResolver) -> RenderedPage:
    """Render ``page`` to HTML for one delivery surface.

    Args:
        page: The page to render.
        manual: The loaded manual (for cross-link resolution).
        resolver: Surface-specific URL builder.

    Returns:
        The :class:`RenderedPage`.
    """
    result = RenderedPage(html="")
    tokens = _MD.parse(page.body, {})
    _apply_callouts(tokens)

    used: dict[str, int] = {}
    section_anchor, section_heading = "", page.title
    section_text: list[str] = []

    def flush() -> None:
        text = " ".join(" ".join(section_text).split())
        if text or section_anchor:
            result.sections.append(Section(section_anchor, section_heading, text))

    for idx, tok in enumerate(tokens):
        if tok.type == "heading_open":
            level = int(tok.tag[1])
            inline = tokens[idx + 1]
            text = _inline_text(inline)
            if level == 1:
                continue
            base = slugify(text) or "section"
            count = used.get(base, 0)
            used[base] = count + 1
            anchor = base if count == 0 else f"{base}-{count}"
            tok.attrSet("id", anchor)
            result.anchors.add(anchor)
            if level <= 3:
                result.toc.append(TocEntry(level, anchor, text))
                flush()
                section_anchor, section_heading, section_text = anchor, text, []
            if level <= 4:
                link = Token("html_inline", "", 0)
                link.content = (
                    f' <a class="heading-link" href="#{anchor}" '
                    f'aria-label="Link to this section">#</a>'
                )
                inline.children = [*(inline.children or []), link]
        elif tok.type == "dt_open" and idx + 1 < len(tokens) and tokens[idx + 1].type == "inline":
            term = _inline_text(tokens[idx + 1])
            anchor = slugify(term) or "term"
            if anchor in used:
                anchor = f"{anchor}-{used[anchor]}"
            used[anchor] = used.get(anchor, 0) + 1
            tok.attrSet("id", anchor)
            result.anchors.add(anchor)
            section_text.append(term)
        if tok.type == "inline":
            prev = tokens[idx - 1] if idx else None
            if not (prev is not None and prev.type == "heading_open"):
                section_text.append(_inline_text(tok))
            for child in tok.children or []:
                if child.type == "link_open":
                    href = str(child.attrGet("href") or "")
                    new = _resolve_target(page, href, manual, resolver, result, is_image=False)
                    child.attrSet("href", new)
                    if _SCHEME_RE.match(new) and not new.startswith("mailto:"):
                        child.attrSet("class", "external")
                        child.attrSet("rel", "noopener noreferrer")
                elif child.type == "image":
                    src = str(child.attrGet("src") or "")
                    child.attrSet(
                        "src", _resolve_target(page, src, manual, resolver, result, is_image=True)
                    )
                    child.attrSet("loading", "lazy")
        elif tok.type in {"fence", "code_block"}:
            section_text.append(tok.content)
        elif tok.type == "html_block":
            tok.content = _rewrite_html(tok.content, page, manual, resolver, result)
            section_text.append(re.sub(r"<[^>]+>", " ", tok.content))
        if tok.type == "inline":
            for child in tok.children or []:
                if child.type == "html_inline":
                    child.content = _rewrite_html(child.content, page, manual, resolver, result)
    flush()
    result.html = _MD.renderer.render(tokens, _MD.options, {})
    return result


def render_markdown(text: str) -> str:
    """Render a standalone Markdown snippet (summaries, card blurbs)."""
    return str(_MD.renderInline(text))


def check_links(manual: Manual, resolver: LinkResolver | None = None) -> list[str]:
    """Return every broken link / anchor across the manual (empty = clean).

    Args:
        manual: The loaded manual.
        resolver: Optional resolver; a neutral one is used when omitted.

    Returns:
        Sorted human-readable problem descriptions.
    """
    resolver = resolver or _NeutralResolver()
    rendered = {key: render_page(page, manual, resolver) for key, page in manual.pages.items()}
    problems: list[str] = []
    for key, page_result in rendered.items():
        problems.extend(page_result.broken)
        for target, anchor in page_result.page_links:
            if anchor and anchor not in rendered[target].anchors:
                problems.append(f"{key}: anchor #{anchor} not found on {target}")
    return sorted(set(problems))


class _NeutralResolver:
    """Resolver used for link checking only."""

    def page_url(self, key: str, anchor: str = "") -> str:
        return f"/{key}" + (f"#{anchor}" if anchor else "")

    def asset_url(self, relpath: str) -> str:
        return f"/assets/{relpath}"


#: Factory type used by surfaces that build a resolver per page.
ResolverFactory = Callable[[Page | None], LinkResolver]
