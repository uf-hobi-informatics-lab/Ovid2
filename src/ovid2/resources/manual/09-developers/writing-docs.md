---
title: Writing documentation
summary: How this manual is built, how to add or edit a page, and the checks that keep it correct.
audience: [developer]
---

## Where the source lives

```text
src/ovid2/resources/manual/
├── manual.yaml               title, audiences, ordered chapters → pages
├── 00-welcome/index.md       one Markdown file per page
├── …
├── 10-reference/*.md         GENERATED (except index.md and environment.md)
└── assets/*.svg              images and diagrams
```

The manual ships inside the wheel, so the panel can serve it at
`/manual`. The same files build the static site.

## Add a page

1. Create `src/ovid2/resources/manual/<chapter>/<slug>.md` with
   front-matter:

   ```markdown
   ---
   title: My page
   summary: One sentence for cards, search and the page lead.
   audience: [user, developer]          # user | researcher | developer | admin
   ---

   Body in Markdown…
   ```

2. List `<slug>` under its chapter in `manual.yaml`. A page that isn't
   listed is an *orphan*, and `ovid2 manual check` fails.
3. Preview it. See [Previewing while you write](#previewing-while-you-write).

## Syntax

| Feature | Write |
| --- | --- |
| Link to another page | `[Engines](../02-concepts/engines.md)`, a path relative to the file |
| Link to a section | `[Overwrite](../04-running-jobs/overwrite-and-batches.md#the-overwrite-gate)` |
| Image | `![Alt](../assets/diagram.svg)` (must be under `assets/`) |
| Callout | `> [!NOTE]`, `[!TIP]`, `[!IMPORTANT]`, `[!WARNING]`, `[!CAUTION]` |
| Definition list | `Term` on one line, then `: definition` on the next |
| Card grid | `<div class="cards"><a class="card" href="x.md"><strong>…</strong><span>…</span></a></div>` |
| Keyboard key | `<kbd>Ctrl</kbd>` |

Links use `.md` paths, so pages also read well in a code browser. The
renderer rewrites them for each surface. Section anchors are GitHub-style
slugs of the heading text.

## The generated reference

`ovid2 manual gen-reference` rewrites `10-reference/*.md` (except `index`
and `environment`) from the code:

- the argparse tree;
- the OpenAPI snapshot;
- the MCP tool modules;
- the run-kind, transform, mapper-op, adapter, fixer, driver and engine
  registries;
- the CDM and vocabulary YAML.

**Never hand-edit those pages.** Improve the docstring or help text in the
code, then regenerate.

## Checks

```bash
ovid2 manual check                 # pages load; links, anchors and assets resolve; no orphans; reference fresh
ovid2 manual gen-reference --check # report stale generated pages without writing (exit 1)
ovid2 manual build --out site      # strict build; fails on any broken link
```

`tests/contract/test_manual.py` runs the same checks on every commit:

| Test | Fails when |
| --- | --- |
| `test_manual_loads` | `manual.yaml` or a page doesn't load, there are fewer than 10 chapters, a page has no title, or a page body is empty or `TODO` |
| `test_manual_links_and_anchors_resolve` | an internal link, section anchor or asset doesn't resolve |
| `test_manual_has_no_orphan_pages` | a page exists that `manual.yaml` doesn't list |
| `test_manual_reference_is_fresh` | `10-reference/*` differs from what `ovid2 manual gen-reference` would write |
| `test_manual_is_site_neutral` | a `.md`, `.yaml` or `.svg` line matches the public mirror's `[leak_gate].deny` patterns or names a denied site pipeline (`scripts/public/manifest.toml`) |

## Previewing while you write

| Command | What it does |
| --- | --- |
| `OVID2_MANUAL_RELOAD=1 ovid2 web` | The panel re-reads the manual sources (and its search index) on every request, so an edit shows on reload. Without it, the manual is loaded once per process. |
| `ovid2 manual serve [--port 8765] [--host 127.0.0.1]` | Builds the static site into a temporary directory and serves it. |

## The GitHub wiki

```bash
ovid2 manual wiki --out <dir>      # default --out wiki; --no-strict exports despite broken links
```

The command exports the manual as a GitHub wiki tree:

- one `<Page-Name>.md` per page, named from its title
  (*Connections & addresses* → `Connections-addresses`), in reading order,
  each starting with its summary and ending with Previous / Next links;
- `Home.md` (the landing page), `_Sidebar.md` (chapter → page tree) and
  `_Footer.md` (a generated-from-source notice);
- images copied to `assets/` and linked relatively.

Manual-only syntax becomes plain GitHub Markdown: `.md` links become wiki
page links, card grids become bullet lists, definition lists become
bold-term bullets, and callouts become GitHub alerts. Code fences and
inline code are never rewritten.

Existing `*.md` files and `assets/` in `<dir>` are replaced. Anything else,
such as `.git`, is left alone, so `<dir>` can be a wiki clone. By default
the export fails on a broken link or image, or when two page titles map to
the same wiki name.

The public-mirror publish runs this export on the sanitised tree after
every push of `main`, then pushes the result to the GitHub wiki. Never
edit the wiki directly, because the next publish overwrites it. Edit the
source here instead.

## Writing style

- Lead with what the reader wants to do. Put background second.
- One idea per paragraph. Use tables for anything with more than two
  attributes.
- Use real commands that work when pasted, with placeholders in
  `<angle-brackets>`.
- Keep examples site-neutral: `demo`, `example.org`, `<account>`.
