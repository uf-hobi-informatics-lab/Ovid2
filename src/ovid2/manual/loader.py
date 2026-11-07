"""Load the manual tree: ``manual.yaml`` + one Markdown file per page.

Layout under :func:`manual_root` (``src/ovid2/resources/manual/``)::

    manual.yaml                 # title, audiences, ordered chapters → pages
    <chapter>/<page>.md         # YAML front-matter + Markdown body
    assets/<file>               # images / diagrams referenced by pages

Each page is addressed by a *key* ``"<chapter>/<page>"`` (no extension).
The first page listed in a chapter is the chapter's landing page. Page
front-matter keys:

- ``title`` (required) — page heading and nav label.
- ``summary`` — one-sentence description (index cards, search, meta).
- ``audience`` — any of the audience slugs declared in ``manual.yaml``.
- ``generated`` — ``true`` for pages written by ``ovid2 manual
  gen-reference`` (never hand-edit them).

The loader is pure: it never renders HTML. Rendering is lazy and cached
per page (:meth:`Page.html`), so the web tier pays for a page only when
it is requested.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "Audience",
    "Chapter",
    "Manual",
    "ManualError",
    "Page",
    "load_manual",
    "manual_root",
]

_FRONT_MATTER_FENCE = "---"


class ManualError(ValueError):
    """Raised when ``manual.yaml`` or a page is malformed."""


def manual_root() -> Path:
    """Return the packaged manual source directory."""
    return Path(__file__).resolve().parent.parent / "resources" / "manual"


@dataclass(frozen=True)
class Audience:
    """A reader persona (``user``, ``researcher``, ``developer``, …).

    Attributes:
        slug: Identifier used in page front-matter.
        label: Human-readable name.
        description: One-line pitch shown on the landing page.
        start: Recommended first pages (page keys) for this reader.
    """

    slug: str
    label: str
    description: str
    start: tuple[str, ...] = ()


@dataclass
class Page:
    """One manual page.

    Attributes:
        key: ``"<chapter>/<page>"`` identifier (also the URL path).
        chapter_slug: Owning chapter.
        slug: Page slug within the chapter.
        path: Absolute path of the Markdown source.
        title: Front-matter title.
        summary: Front-matter summary ('' when absent).
        audience: Audience slugs.
        generated: ``True`` for registry-generated reference pages.
        body: Markdown body (front-matter stripped).
        meta: The full front-matter mapping.
        prev_key / next_key: Reading-order neighbours (whole manual).
    """

    key: str
    chapter_slug: str
    slug: str
    path: Path
    title: str
    summary: str
    audience: tuple[str, ...]
    generated: bool
    body: str
    meta: dict[str, Any] = field(default_factory=dict)
    prev_key: str | None = None
    next_key: str | None = None

    @property
    def source_relpath(self) -> str:
        """Return the page source path relative to the manual root."""
        return f"{self.key}.md"


@dataclass
class Chapter:
    """An ordered group of pages.

    Attributes:
        slug: Directory name under the manual root.
        title: Display title.
        summary: One-line description for the chapter index.
        number: 1-based display number (``01``, ``02``…).
        pages: Pages in reading order; ``pages[0]`` is the landing page.
    """

    slug: str
    title: str
    summary: str
    number: int
    pages: list[Page] = field(default_factory=list)

    @property
    def landing(self) -> Page:
        """Return the chapter's first page."""
        return self.pages[0]

    @property
    def audience(self) -> tuple[str, ...]:
        """Return the union of page audiences, in declaration order."""
        seen: list[str] = []
        for page in self.pages:
            for aud in page.audience:
                if aud not in seen:
                    seen.append(aud)
        return tuple(seen)


@dataclass
class Manual:
    """The whole loaded manual.

    Attributes:
        root: Source directory.
        title: Manual title.
        tagline: Subtitle shown on the landing page.
        edit_base_url: Prefix for "Edit this page" links ('' disables).
        audiences: Declared audiences, keyed by slug.
        chapters: Chapters in order.
        pages: Every page keyed by ``"<chapter>/<page>"``.
    """

    root: Path
    title: str
    tagline: str
    edit_base_url: str
    audiences: dict[str, Audience]
    chapters: list[Chapter]
    pages: dict[str, Page]
    _html_cache: dict[str, Any] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def chapter(self, slug: str) -> Chapter | None:
        """Return the chapter called ``slug`` or ``None``."""
        for chapter in self.chapters:
            if chapter.slug == slug:
                return chapter
        return None

    def page(self, key: str) -> Page | None:
        """Return the page with ``key`` (``"<chapter>/<page>"``) or ``None``."""
        return self.pages.get(key)

    def ordered_pages(self) -> list[Page]:
        """Return every page in reading order."""
        return [page for chapter in self.chapters for page in chapter.pages]

    @cached_property
    def asset_dir(self) -> Path:
        """Return the directory holding images / diagrams."""
        return self.root / "assets"

    def cached(self, key: str, factory: Any) -> Any:
        """Memoise ``factory()`` under ``key`` (thread-safe)."""
        with self._lock:
            if key not in self._html_cache:
                self._html_cache[key] = factory()
            return self._html_cache[key]


def split_front_matter(text: str, *, source: str = "<string>") -> tuple[dict[str, Any], str]:
    """Split ``text`` into ``(front_matter, body)``.

    Args:
        text: Full file contents.
        source: Label used in error messages.

    Returns:
        The parsed front-matter mapping (``{}`` when absent) and the body.

    Raises:
        ManualError: The front-matter block is unterminated or not a mapping.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != _FRONT_MATTER_FENCE:
        return {}, text
    for idx in range(1, len(lines)):
        if lines[idx].strip() == _FRONT_MATTER_FENCE:
            raw = "".join(lines[1:idx])
            body = "".join(lines[idx + 1 :])
            data = yaml.safe_load(raw) or {}
            if not isinstance(data, dict):
                raise ManualError(f"{source}: front-matter must be a mapping")
            return data, body.lstrip("\n")
    raise ManualError(f"{source}: unterminated front-matter block")


def _as_str(value: Any, *, where: str, required: bool = False) -> str:
    if value is None:
        if required:
            raise ManualError(f"{where}: missing required value")
        return ""
    if not isinstance(value, str):
        raise ManualError(f"{where}: expected a string, got {type(value).__name__}")
    return value.strip()


def _load_page(root: Path, chapter_slug: str, slug: str, audiences: dict[str, Audience]) -> Page:
    key = f"{chapter_slug}/{slug}"
    path = root / chapter_slug / f"{slug}.md"
    if not path.is_file():
        raise ManualError(f"manual.yaml lists {key!r} but {path} does not exist")
    meta, body = split_front_matter(path.read_text(encoding="utf-8"), source=key)
    audience_raw = meta.get("audience") or []
    if isinstance(audience_raw, str):
        audience_raw = [audience_raw]
    unknown = [a for a in audience_raw if a not in audiences]
    if unknown:
        raise ManualError(f"{key}: unknown audience(s) {unknown}; declare them in manual.yaml")
    return Page(
        key=key,
        chapter_slug=chapter_slug,
        slug=slug,
        path=path,
        title=_as_str(meta.get("title"), where=f"{key}: title", required=True),
        summary=_as_str(meta.get("summary"), where=f"{key}: summary"),
        audience=tuple(audience_raw),
        generated=bool(meta.get("generated", False)),
        body=body,
        meta=meta,
    )


def load_manual(root: Path | None = None) -> Manual:
    """Load and validate the manual tree.

    Args:
        root: Manual source directory; defaults to :func:`manual_root`.

    Returns:
        The loaded :class:`Manual` with reading-order links filled in.

    Raises:
        ManualError: ``manual.yaml`` is missing / malformed, a listed page
            is missing, a page is listed twice, or a page uses an
            undeclared audience.
    """
    root = (root or manual_root()).resolve()
    index_path = root / "manual.yaml"
    if not index_path.is_file():
        raise ManualError(f"{index_path} not found")
    spec = yaml.safe_load(index_path.read_text(encoding="utf-8")) or {}
    if not isinstance(spec, dict):
        raise ManualError("manual.yaml must be a mapping")

    audiences: dict[str, Audience] = {}
    for slug, info in (spec.get("audiences") or {}).items():
        info = info or {}
        audiences[str(slug)] = Audience(
            slug=str(slug),
            label=_as_str(info.get("label"), where=f"audience {slug}: label") or str(slug),
            description=_as_str(info.get("description"), where=f"audience {slug}: description"),
            start=tuple(str(k) for k in (info.get("start") or [])),
        )

    chapters: list[Chapter] = []
    pages: dict[str, Page] = {}
    for number, raw in enumerate(spec.get("chapters") or [], start=1):
        if not isinstance(raw, dict):
            raise ManualError(f"chapter #{number}: expected a mapping")
        slug = _as_str(raw.get("slug"), where=f"chapter #{number}: slug", required=True)
        chapter = Chapter(
            slug=slug,
            title=_as_str(raw.get("title"), where=f"chapter {slug}: title", required=True),
            summary=_as_str(raw.get("summary"), where=f"chapter {slug}: summary"),
            number=number,
        )
        page_slugs = raw.get("pages") or []
        if not page_slugs:
            raise ManualError(f"chapter {slug}: must list at least one page")
        for page_slug in page_slugs:
            page = _load_page(root, slug, str(page_slug), audiences)
            if page.key in pages:
                raise ManualError(f"page {page.key!r} listed twice")
            pages[page.key] = page
            chapter.pages.append(page)
        chapters.append(chapter)

    for aud in audiences.values():
        missing = [k for k in aud.start if k not in pages]
        if missing:
            raise ManualError(f"audience {aud.slug}: start page(s) {missing} not found")

    ordered = [p for c in chapters for p in c.pages]
    for prev, cur in itertools.pairwise(ordered):
        prev.next_key = cur.key
        cur.prev_key = prev.key

    return Manual(
        root=root,
        title=_as_str(spec.get("title"), where="title") or "Manual",
        tagline=_as_str(spec.get("tagline"), where="tagline"),
        edit_base_url=_as_str(spec.get("edit_base_url"), where="edit_base_url"),
        audiences=audiences,
        chapters=chapters,
        pages=pages,
    )


def orphan_pages(manual: Manual) -> list[str]:
    """Return Markdown files under the root that ``manual.yaml`` does not list."""
    listed = {page.path.resolve() for page in manual.pages.values()}
    orphans: list[str] = []
    for path in sorted(manual.root.rglob("*.md")):
        if path.resolve() in listed or "assets" in path.relative_to(manual.root).parts:
            continue
        orphans.append(path.relative_to(manual.root).as_posix())
    return orphans
