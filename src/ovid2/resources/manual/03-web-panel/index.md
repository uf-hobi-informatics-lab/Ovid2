---
title: The web control panel
summary: A guided tour of the browser UI — layout, navigation, keyboard shortcuts and what each section is for.
audience: [user, admin]
---

`ovid2 web` serves the control panel (default `http://127.0.0.1:8090`).
Everything the panel does goes through the same `/api/v1` REST API that
the MCP server and scripts use, so nothing is possible only in the browser.

## Layout

| Region | Contents |
| --- | --- |
| **Top bar** | the Ovid2 wordmark (home), the page title on narrow screens, the light/dark toggle |
| **Left drawer** | navigation in five groups, collapsible to an icon rail |
| **Main area** | the current page |
| **Right rail** | the command palette, the *Appearance* panel, and the *Agent Ovid* assistant |

The drawer adapts to the screen: it shows full labels on wide screens, an
icon rail on medium ones, and a hamburger menu on phones.

## Navigation map

Which links you see depends on your [role](../08-operations/auth.md).

| Group | Page | What it is for | Guide |
| --- | --- | --- | --- |
| Operate | Dashboard | queue, recent runs, environment checks | [Dashboard](dashboard.md) |
| | Submit | the graphical job builder | [Submit](submit.md) |
| | Pipelines | saved configs, packages, formatters, mappers | [Pipelines](pipelines.md) |
| | Navigator | every pipeline → table → field → function, and UDFs | [Pipeline navigator](navigator.md) |
| | Jobs | history, live run pages, reports | [Jobs & runs](jobs.md) |
| Explore | Data Space | browse, inspect and export any table | [Data Space](dataspace.md) |
| | Reports | design, organise, share and export research reports | [Report Studio](../05-data/report-studio.md) |
| Configure | Storage accounts | connections and addresses | [Storage & warehouses](storage-and-warehouses.md) |
| | Warehouses | compute engines | [Storage & warehouses](storage-and-warehouses.md) |
| | LLMs, Secrets, CDMs, Settings, Setup wizard | configuration | [Configure](configure.md) |
| Access | Users | activation and roles | [Administration](administration.md) |
| System | Health, **Manual**, API | diagnostics, this manual, Swagger UI | — |

## Keyboard

| Keys | Action |
| --- | --- |
| <kbd>Ctrl</kbd>/<kbd>⌘</kbd> + <kbd>K</kbd> | command palette: jump to any page by name |
| <kbd>/</kbd> | in the manual: focus search |
| <kbd>↑</kbd> <kbd>↓</kbd> <kbd>→</kbd> <kbd>←</kbd> <kbd>Enter</kbd> | navigate and select in the storage trees |
| <kbd>Esc</kbd> | close a dialog, palette or tooltip |

## Help tips

A small **?** next to a heading or field opens an explanation. Hover over
it, focus it with the keyboard, or tap it on a phone. Pages keep their
descriptions there, so the forms themselves stay short.

## Forms only show what applies

When you create, edit or run a pipeline, the form shows only the fields
that matter for the job type, engine and trigger you picked. For example,
*Workers* disappears for copy / move and Snowpark, and *Transformations*
disappears for data-health jobs. Rarely used options, such as mirroring
events to stderr or extra bind mounts, sit under **Advanced**. When a field
must be one of a known set (a CDM, engine, table, column, file format,
role or secret file), it is a drop-down list rather than a text box.

## Pages in this chapter

<div class="cards">
  <a class="card" href="dashboard.md"><strong>Dashboard</strong><span>The at-a-glance view.</span></a>
  <a class="card" href="submit.md"><strong>Submit</strong><span>Build and launch a job visually.</span></a>
  <a class="card" href="pipelines.md"><strong>Pipelines</strong><span>Saved configs and pipeline packages.</span></a>
  <a class="card" href="jobs.md"><strong>Jobs & runs</strong><span>History, live canvas, reports.</span></a>
  <a class="card" href="dataspace.md"><strong>Data Space</strong><span>Browse, inspect and export tables.</span></a>
  <a class="card" href="storage-and-warehouses.md"><strong>Storage & warehouses</strong><span>Connections, addresses, engines.</span></a>
  <a class="card" href="configure.md"><strong>Configure</strong><span>LLMs, secrets, CDMs, settings, setup wizard.</span></a>
  <a class="card" href="administration.md"><strong>Administration</strong><span>Users, roles, approvals.</span></a>
  <a class="card" href="appearance.md"><strong>Appearance</strong><span>Themes, colours, fonts.</span></a>
</div>
