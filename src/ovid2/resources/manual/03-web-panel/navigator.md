---
title: Atlas (navigator)
summary: The whole workspace as a board, one line per workflow and per pipeline, fitted to the page — to navigate, edit, run, pause and watch every pipeline without leaving the page.
audience: [user, developer, researcher]
---

**Operate → Navigator** (`/navigator`) is the **Ovid Atlas**: the whole
setup of your workspace drawn as one continuous object. Everyone in the
workspace sees the same Atlas. Nothing on it reads data until you ask for
sample rows.

## The three layers

Ovid2 keeps *where data lives*, *what computes it* and *what is computed*
independent. The Atlas keeps them as three layers:

| Layer | Holds |
| --- | --- |
| **algorithms** | workflows, pipeline packages (formatter → … → fix specs), and the saved pipelines (run configs) |
| **compute** | engines — saved engine configs and the built-in types a pipeline uses |
| **storage** | output locations, input locations, and the storage accounts that hold them |

Every saved pipeline is drawn as one **pipeline component**:

| Part | Shows |
| --- | --- |
| **name** | the saved pipeline's name, its clock, ▶ ‖ ■ and its status |
| **shape** | the run kind and the package it runs, e.g. *ETL · example_pcornet* |
| **material** | the engine type (DuckDB, PySpark, Snowpark, Databricks Spark) |
| **input → output** | the input and output locations, each with its storage logo; *per run* when the location is chosen at run time |

Click a part to open that location, engine or package in the dock. Drag an
engine or a location from one pipeline onto another to re-bind it.

### Workflows and pipelines

The Atlas is a board of **lines** in groups, stacked top to bottom with a
wide gap between groups:

| Group | Shows |
| --- | --- |
| ⛓ **Workflows** | one line per workflow: its pipeline components laid out the way the workflow links them. A *data* link is a solid arrow, an *after* link a dashed one; a failure branch is red (✗), *always* is ∀ and *otherwise* is "else". A step that joins with *any* carries an **any** badge. A pipeline used by two steps shows twice. |
| ▶ **Pipelines** | one line per saved pipeline that no workflow uses |
| ◫ **Storage accounts** | the storage accounts, the way into the Data Space |
| ∅ **Not used** | packages and engines no pipeline uses |

The lines always **fit the page exactly**. They share the canvas height and
are re-fitted when you resize the window, the dock or the side panel. Tall
lines show more on each card: the summary line and, for a running pipeline,
its progress and stage chain. With many lines they shrink to one line each,
and only then does the list scroll, inside the canvas. The page itself never
scrolls.

- **running on top** (bar, key `t`, on by default) floats every line with a
  running, queued or paused job to the top of its group. A workflow counts as running if
  any of its steps is running. Other lines keep their order. Lines glide to
  their new place.
- **Layer buttons** (bar, or keys `1` `2` `3`) show or hide a layer's
  parts of every pipeline component. Double-click a layer button (or Alt-click it) to show only that
  layer.
- **Find** (`/`) dims every card that does not match and outlines the lines
  that hold a match. Press Enter to scroll to and open the first hit.
- **Running** (`r`) scrolls to the first running line and opens its run
  console. **Clear** (`c`) clears the search and highlights and goes back to
  the top.

## Cards and the dock

Cards have a fixed size and never expand in place. Click a card (or press
Enter on it) to **select** it. Everything about it opens in the dock. A
running pipeline opens straight on its run console. Shift-click a card for
its blast radius, and `p` pins the selection.

The **dock** under the canvas shows everything about the selection, in
tabs. Drag its edge to resize it. ⇆ docks it to the right, ▢ maximises it
and ▁ minimises it.

| Tab | Shows |
| --- | --- |
| **overview** | facts, links to the classic pages, recent runs (with **replay**) |
| **run console** | the live run of a pipeline (see below) |
| **lanes** | a package table's fields × stages and the spec editor |
| **data space** | browse a storage account or location; a table's columns, size and row count; **load sample rows** |
| **edit** | the saved pipeline, location, storage account or engine, edited in place |
| **ask steward** | ask Agent Ovid about the selection |

The address bar is a deep link: `/navigator?node=config:<name>&tab=run`.
The old navigator links (`/navigator?p=<package>&t=<table>&f=<FIELD>`) open
that package's lanes.

## Running, pausing, cancelling

On a pipeline card (and in its run console):

- **▶** runs the pipeline now. If its output already holds tables you are
  asked to confirm the overwrite.
- **‖** pauses the run *at the next table boundary*. The table that is
  running finishes first, because its write is already executing. The run
  then holds, its clocks freeze and the card reads *paused*. Health and DQ
  runs hold between checks. One-shot jobs (exports) cannot pause.
- **▶** on a paused run resumes it with the next table.
- **■** cancels the run, paused or not.

These need the submit-jobs capability. The same actions are
`POST /api/v1/jobs/{id}/pause|resume|cancel`, and the MCP tools
`pause_job` / `resume_job` / `cancel_job`.

### What the animation means

- A running pipeline is outlined in orange, and its input → output arrow
  and the workflow links leaving it pulse orange. A paused pipeline is pale.
  A failed one is red.
- The **chain** shows `read › format › deduplicate › transform › map › fix ›
  write` for the table that is running. Finished steps are filled, the
  running step pulses, and pending steps are hollow.
- **Dashed steps** only build the engine's *lazy plan*, so they finish
  almost instantly. The engine does the real work during **write**, and the
  write's clock is the table's real time.
- Clocks use the server's times, so every member sees the same numbers.

### Replay

**replay** on a finished run (overview → recent runs) plays the run back on
its line and run console from its saved timings. Drag the scrubber to move through it;
**live** goes back.

## Editing in place

The **edit** tab edits the selected object with the same checks as its own
page:

- **pipeline** — input, engine, output, trigger, enabled, description.
  Changing the engine or a location is checked first (see drag-to-rebind).
- **location** — its storage account and location fields.
- **storage account / engine** — their catalogue fields. Secrets show as
  `***`; leave them to keep the stored value.
- **workflow** — opens its DAG canvas.

Saving needs the manage-config capability. If another member has the same
object open in an editor, you see *"X is editing this right now"*. This is a
soft lock: you can still save.

### Drag to rebind

Drag an **engine** or a **location** card onto a **pipeline** card to
re-bind it. For a location you are asked whether it is the input or the
output. Before anything is saved the drop is checked:

- the engine has every capability the package's steps need;
- an output is not read-only storage (Microsoft Access);
- the engine can read and write those storage types.

A refused drop shakes red and the reason is shown at the bottom right.

## Field lanes and specs

In a package's **lanes** tab, hover a field to see its **blast radius**: every
step that reads it and every field those steps write, transitively. Click a
chip and the spec editor opens beside the lanes:

- **Step details** shows what the step writes and reads, the reference
  tables it uses, and the document and path that define it.
- **The editor** is the same one the [pipeline builder](pipelines.md)
  uses, with **Form** and **YAML** tabs and live checks. It opens the
  step's formatter, mapper, transform chain, assignments or fixer chain,
  scrolled to that step.

Saving refreshes the tree and the lanes. Python sources open in a
read-only viewer at the matching line. These are custom formatters,
generated legacy formatters and vendored legacy fixer scripts.

Legacy-parity ports are **generated**. Change them by regenerating, never
by hand-editing (see `docs/parity.md`), so every step of a `legacy_ovid`
package is read-only here.

## Applying your own UDF

On an editable package, **+ UDF for &lt;table&gt;** (above the lanes) or
**+ƒ** / **Apply UDF to &lt;FIELD&gt;** opens the UDF dialog. A UDF is
*declarative*: library functions and a checked Spark-SQL expression.
Python UDFs are not supported.

| Kind | You give |
| --- | --- |
| **Computed column** | a column and a row expression, e.g. `coalesce(nullif(upper(trim(SEX)), ''), 'UN')`. The expression is checked as you type against the allow-list of built-in functions: no sub-queries, aggregates or unknown functions. A warning flags names that are not columns of the table. Column and function names autocomplete. |
| **Chain of functions** | `trim`, `to_upper`, `regex_replace`, `null_if`, `default_value`, `cast`, …, with the field pre-selected. |
| **Existing chain** | a chain from your library, the shipped library or this package. **Used by** lists every pipeline · table it is assigned to and the fields it writes. |

Then choose where the UDF is kept:

- **inline** — an anonymous entry in this package's
  `transforms/assignments.yaml`;
- **private** — a named chain in `transforms/<name>.yaml`;
- **library** — your transform library (`$XDG_CONFIG_HOME/ovid2/transforms`),
  reusable by every pipeline.

Finally pick the scope: **this table** or **every table** (`*`). UDFs run
in the *transform* stage, after deduplicate and before map. Applying needs
the manage-config capability. Everything is validated before anything is
written. The same action is available to agents as
`POST /api/v1/pipeline-packages/{p}/udfs:apply`.

## Overlays

**overlays** (bar) adds these views to the board:

| Overlay | Shows |
| --- | --- |
| **heat** | median run time and failures per pipeline and per table over 30 days |
| **data health** | the latest failed health checks pinned to the locations and tables they flag. In the lanes, a *fix:* button opens the fix chain the check recommends |
| **changed since last success** | anything edited after a pipeline last ran cleanly — the pipeline, its package, its engine, its locations or their storage account |
| **who is here** | avatars of the other members, in the board's top corner and on the cards they look at. Orange means editing |

**Blast radius**: shift-click a card (or hold Alt while hovering it) to dim
everything that is *not* downstream of it. Esc clears it.

**Saved views** keep the layers, *running on top*, pinned cards, overlays
and dock size. Everyone in the workspace can open them. Saving one needs
manage-config. You can delete only the views you created.

## Ask the Steward

Pin cards with `p` to add them to the context, then use **ask steward**.
Agent Ovid reads the selection through the `atlas_subgraph` tool. This
gives it metadata and links only, never row values or secrets. Anything it
proposes appears in the assistant panel, and the cards it would touch get a
dashed purple outline. Nothing runs until you click **Run** on the proposal.

## Packages that are listed

A package appears when a saved pipeline uses it, or when it is the source of
a pipeline template. To delete a package, use its package hub (see
[Delete a package](pipelines.md#delete-a-package)) or
`ovid2 pipeline delete <name>`.

## For agents

| Tool | Returns |
| --- | --- |
| `atlas_subgraph` | the Atlas around some nodes |
| `atlas_live` | what runs now, per table and stage |
| `inspect_pipeline_lineage` | one package's field lineage |

Pausing and resuming are proposals (`pause_job`, `resume_job`). The REST
surface is `/api/v1/atlas/*` — see the [API reference](../10-reference/api.md).
