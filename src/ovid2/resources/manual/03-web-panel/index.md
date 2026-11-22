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
| **Top bar** | the animated Ovid2 mark and wordmark (home link), your user chip and *Logout* |
| **Left drawer** | the menu button, then navigation in five groups (*Operate*, *Explore*, *Configure*, *Access*, *System*); collapsible to an icon rail |
| **Main area** | the current page |
| **Right rail** | the command palette, the *Appearance* panel, and the *Agent Ovid* assistant |

The drawer adapts to the screen. On wide and medium screens the **menu
button** (☰) sits at the top of the drawer and switches between full labels
and a compact icon rail. The button stays in the same place either way, and
the choice is remembered. On phones the drawer is hidden, and the menu
button is in the top bar.

### The Ovid2 mark

The leaf mark next to *Ovid2* shows pipeline activity:

| Mark | Meaning |
| --- | --- |
| breathing gently, blue gradient | nothing is running |
| leaves lighting up in turn (O → I → D) | a job is running or queued |
| left / centre / right leaf highlighted | the job on this page is reading, transforming or writing |
| green rustle | the job on this page finished |
| amber stem | a data-quality check on this job reported exceptions |
| red, drooping | the job on this page failed |

On a job page the mark follows that job. On every other page it shows
whether *any* job you can see is running. The browser-tab icon follows the
same colours. If your operating system asks for reduced motion, the mark
changes colour but does not move.

### The left menu

The menu is compact, so the whole list fits on a typical laptop screen.
The *Navigation* size in the [Appearance](appearance.md) panel still
overrides the default text size.

When the menu is taller than the window, the list scrolls on its own and
fades at the top or bottom edge. A **▾ N more** button at the bottom
counts the items hidden below the fold; click it to scroll them into
view. On load, the menu scrolls so the current page's item is visible.

### Assistant panel

The **Agent Ovid** button on the right rail opens the assistant. Its
header has these controls:

- **Session history** opens the list of saved conversations;
- **Search sessions** searches their titles and messages;
- the **context meter** shows how much of the model's context the last
  turn used;
- **New session** starts a fresh conversation.

Every conversation is saved on the server as a session. See
[Sessions, history & context](../07-assistant/sessions.md).

## Navigation map

Which links you see depends on your [role](../08-operations/auth.md).

| Group | Page | What it is for | Guide |
| --- | --- | --- | --- |
| Operate | Dashboard | queue, recent runs, environment checks | [Dashboard](dashboard.md) |
| | Submit | the graphical job builder | [Submit](submit.md) |
| | Pipelines | saved pipeline configs: run, edit, schedule | [Pipelines](pipelines.md) |
| | Workflows | chain pipelines on a canvas; triggers, runs, rollback | [Workflows](workflows.md) |
| | Packages | installed pipeline packages, the pipeline builder, **Delete package** | [Pipeline packages](pipelines.md#pipeline-packages) |
| | Navigator | every pipeline → table → field → function, and UDFs | [Pipeline navigator](navigator.md) |
| | Transforms | the reusable transform library (UDFs) | [Transform library](pipelines.md#transform-library) |
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
  <a class="card" href="workflows.md"><strong>Workflows</strong><span>Chain pipelines on a canvas.</span></a>
  <a class="card" href="navigator.md"><strong>Navigator</strong><span>Field-level lineage and UDFs.</span></a>
  <a class="card" href="jobs.md"><strong>Jobs & runs</strong><span>History, live canvas, reports.</span></a>
  <a class="card" href="dataspace.md"><strong>Data Space</strong><span>Browse, inspect and export tables.</span></a>
  <a class="card" href="storage-and-warehouses.md"><strong>Storage & warehouses</strong><span>Connections, addresses, engines.</span></a>
  <a class="card" href="configure.md"><strong>Configure</strong><span>LLMs, secrets, CDMs, settings, setup wizard.</span></a>
  <a class="card" href="administration.md"><strong>Administration</strong><span>Users, roles, approvals.</span></a>
  <a class="card" href="appearance.md"><strong>Appearance</strong><span>Themes, colours, fonts.</span></a>
</div>
