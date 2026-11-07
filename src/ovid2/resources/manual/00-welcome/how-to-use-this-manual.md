---
title: How to use this manual
summary: Reading paths, page conventions, badges, callouts, and where the manual lives.
audience: [user, researcher, developer, admin]
---

## Structure

The manual is twelve numbered chapters. Chapters build on each other, but
every page is self-contained and links to what it assumes.

| # | Chapter | Read it when you want to… |
| --- | --- | --- |
| 00 | [Welcome](index.md) | get oriented |
| 01 | [Getting started](../01-getting-started/index.md) | install Ovid2 and finish a first run |
| 02 | [Core concepts](../02-concepts/index.md) | understand the mental model |
| 03 | [Web control panel](../03-web-panel/index.md) | learn the browser UI page by page |
| 04 | [Running jobs](../04-running-jobs/index.md) | run ETL, health checks, copies, exports, schedules |
| 05 | [Data & data models](../05-data/index.md) | understand and trust the output (researchers) |
| 06 | [Authoring pipelines](../06-authoring/index.md) | describe a new data source |
| 07 | [Assistant & agents](../07-assistant/index.md) | use the LLM assistant or Agent Ovid |
| 08 | [Operations & security](../08-operations/index.md) | deploy, secure and maintain a server |
| 09 | [Developer guide](../09-developers/index.md) | change Ovid2 itself |
| 10 | [Reference](../10-reference/index.md) | look up every command, endpoint, op and option |
| 11 | [Help](../11-help/index.md) | fix a problem |

## Audience badges

Each page carries one or more badges under its title:

- <span class="om-badge om-badge-user">Operator</span> — runs jobs day to day.
- <span class="om-badge om-badge-researcher">Researcher</span> — consumes and evaluates the data.
- <span class="om-badge om-badge-developer">Developer</span> — authors pipelines or extends the framework.
- <span class="om-badge om-badge-admin">Administrator</span> — deploys and secures a server.
- <span class="om-badge om-badge-generated">Generated</span> — produced from the code; always current.

## Conventions

- `monospace` marks commands, file paths, field names and literal values.
- `<angle-brackets>` are placeholders you replace, e.g. `<account>`.
- `$XDG_CONFIG_HOME` defaults to `~/.config`; `$XDG_STATE_HOME` to
  `~/.local/state`. See [Configuration](../08-operations/configuration.md).
- Shell examples assume a POSIX shell with `ovid2` on your `PATH`.

Callouts highlight things worth pausing for:

> [!NOTE]
> Background or a detail that is useful but not essential.

> [!TIP]
> A shortcut or a better way to do something.

> [!WARNING]
> Something that can make a run fail or produce surprising results.

> [!CAUTION]
> Something that can destroy or expose data. Read before proceeding.

## Where the manual lives

The manual is written once, as Markdown under
`src/ovid2/resources/manual/`, and delivered in two forms:

- **Inside the web panel** at `/manual` (left navigation → *System →
  Manual*). It follows your appearance theme and is available to every
  signed-in user.
- **As a static site** built with `ovid2 manual build --out site/`. The
  output is self-contained HTML that works offline, from `file://`, or on
  any static host.

The [Reference](../10-reference/index.md) chapter is generated from the
code itself, so it never drifts. To contribute a page, see
[Writing documentation](../09-developers/writing-docs.md).
