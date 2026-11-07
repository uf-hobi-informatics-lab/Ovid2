"""The Ovid2 Manual — one Markdown source, two delivery surfaces.

The manual's pages live as Markdown files with YAML front-matter under
``src/ovid2/resources/manual/`` (so they ship inside the wheel) and are
ordered by ``resources/manual/manual.yaml``. This package turns that tree
into HTML:

- :mod:`ovid2.manual.loader` — reads ``manual.yaml`` + every page, builds
  the chapter tree, previous / next links and the page index.
- :mod:`ovid2.manual.render` — Markdown → HTML (heading anchors, table of
  contents, GitHub-style callouts, syntax highlighting, cross-page link
  rewriting) plus a link checker.
- :mod:`ovid2.manual.search` — the client-side search index.
- :mod:`ovid2.manual.static_site` — renders the whole manual to a
  self-contained static site (``ovid2 manual build``).
- :mod:`ovid2.manual.reference` — regenerates the *Reference* chapter
  from the live registries (``ovid2 manual gen-reference``). Its heavy
  imports are function-local; the web tier never imports it.

The web control panel serves the same pages at ``/manual`` through
:mod:`ovid2.web.routes.manual_html`. Everything imported at module level
here is web-safe: ``markdown-it-py``, ``mdit-py-plugins``, ``pygments``
and ``pyyaml`` only.
"""

from __future__ import annotations

from ovid2.manual.loader import Chapter, Manual, ManualError, Page, load_manual, manual_root

__all__ = ["Chapter", "Manual", "ManualError", "Page", "load_manual", "manual_root"]
