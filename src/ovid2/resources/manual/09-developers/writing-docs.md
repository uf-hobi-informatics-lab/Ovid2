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
3. Preview it with either:
   - `OVID2_MANUAL_RELOAD=1 ovid2 web`, then open `/manual/<chapter>/<slug>`
     (the page re-reads on every request); or
   - `ovid2 manual serve`.

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
ovid2 manual check            # every page loads; links, anchors and assets resolve; no orphans; reference fresh
ovid2 manual build --out site # strict build; fails on any broken link
```

## The GitHub wiki

`ovid2 manual wiki --out <dir>` exports the manual as a GitHub wiki: one
page per manual page, named from its title, plus `Home`, `_Sidebar` and
`_Footer`. Links, card grids, definition lists and callouts are converted
to plain GitHub Markdown, and images are copied to `assets/`.

The public-mirror publish runs this export on the sanitised tree after
every push of `main`, then pushes the result to the GitHub wiki. Never
edit the wiki directly, because the next publish overwrites it. Edit the
source here instead.

Contract tests run the same checks on every commit:

- `test_manual_links.py`;
- `test_manual_reference_fresh.py`;
- `test_manual_site_neutral.py`, which applies the public leak-gate
  patterns to the manual.

## Writing style

- Lead with what the reader wants to do. Put background second.
- One idea per paragraph. Use tables for anything with more than two
  attributes.
- Use real commands that work when pasted, with placeholders in
  `<angle-brackets>`.
- Keep examples site-neutral: `demo`, `example.org`, `<account>`.
