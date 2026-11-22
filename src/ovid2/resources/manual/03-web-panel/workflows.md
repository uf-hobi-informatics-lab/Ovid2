---
title: Workflows
summary: Chain saved pipelines on a canvas — pass one step's output to the next, run them in order, branch on what a step produced, fan out and join back — triggered by a schedule or a watched folder, with automatic rollback on failure.
audience: [user, admin]
---

**Operate → Workflows** (`/workflows`) chains saved
[pipeline configs](pipelines.md) into one automated run. A workflow is a
small graph:

- each **step** runs one saved pipeline config, optionally with its own
  input, output and tables;
- a **data** link passes a step's *output* in as the next step's *input*.
  Because every address is the
  [exact location](../04-running-jobs/overwrite-and-batches.md) of its data,
  what one step writes is exactly what the next one reads;
- an **after** link only sets the order. The next step keeps its own source
  and destination, so pipelines on completely different storage can be
  sequenced (for example, *load Snowflake, then refresh an S3 export*).

Steps with no link between them run in parallel, up to the worker's
`--max-parallel`. A step may have **several links out**: each is a separate
path, and they run in parallel. A step may have **several links in**: it
waits until all of them have finished. Any link may carry a
[condition](#branching), so a workflow can branch on what a step produced.

```text
 [watch: inbox/landing]──▶ ETL site_x ──data──▶ Copy to warehouse ──after──▶ DQ checks
                                    └────after────▶ Copy archive (own source → own target)

 ETL ──if health.blockers = 0──▶ Copy to warehouse ─┐
     └─────────else───────────▶ Copy to quarantine ─┴─(any)─▶ Notify
     └────────on fail─────────▶ Copy raw to triage
```

## The workflows page

Each workflow is a card showing a miniature of its graph, its trigger, and
the status of its last run. Use the toolbar to filter by name, trigger or
status. The card actions are:

| Action | What it does |
| --- | --- |
| ▶ **Run** | Starts a run now, whatever the trigger, and opens its live view. |
| **Enable / disable** | Switches the trigger on or off. Manual runs still work while it is off. |
| ✎ **Edit** | Opens the workflow on the canvas. |
| ⧉ **Duplicate** | Copies the workflow under a new name. The copy starts **disabled**. |
| 🗑 **Delete** | Removes the definition and keeps its run history. Deleting through the API needs a human [approval](../07-assistant/approvals.md). |

**Recent runs** lists the latest runs of every workflow.

## The canvas

**New workflow**, or a workflow's name, opens the canvas editor. It has
three panes.

**Pipelines (left)**
- Lists every saved pipeline config.
- Search the list, then drag a pipeline onto the canvas or press **+**.
- With a step selected, **+** appends the new step after it, linked by a
  data link.

**Canvas (centre)**
- The **trigger** pill sits in front of the first step(s).
- Each **step** shows:
  - its run kind (colour-coded);
  - its name;
  - its resolved `input → output` (`⇢ step` when the input comes from a data link);
  - a shield when it will be rolled back on failure;
  - a warning mark after **Validate** if something is wrong.
- Drag a step's right-hand port onto another step to link them. A green
  outline accepts the link; red means the link would create a loop.
- Click a link to change its type, its outcome (success / failure / always /
  otherwise) and its condition. Conditional links are drawn amber with an
  *if* chip, *else* links dash-dotted, *on fail* links red.
- Scroll to zoom, drag the background to pan, and drag steps to arrange
  them. **Tidy** lays the graph out left to right; **Fit** frames it.

**Inspector (right)**
- **Nothing selected** — the workflow: name, description and trigger.
- **A step** — its pipeline, label, input and output (selects of saved
  addresses) and, for copy / move steps, its tables. With two or more
  incoming links, **Run when** (*all* / *any* incoming link is taken).
  **Data probes** lists its probes.
- **A link** — *data* or *after*, **Follow when** (the outcome), a branch
  label and the **Condition** builder.
- Fields that do not apply are hidden. For example, the input is read-only
  when a data link feeds the step, and *Tables* appears only for copy /
  move steps.
- **Safety** holds *Overwrite existing data* and *Roll back if the
  workflow fails*.

| Keys | Action |
| --- | --- |
| <kbd>Del</kbd> | remove the selected step or link |
| <kbd>Ctrl</kbd> + <kbd>D</kbd> | duplicate the selected step (the copy needs its own output) |
| <kbd>Ctrl</kbd> + <kbd>Z</kbd> / <kbd>Ctrl</kbd> + <kbd>Y</kbd> | undo / redo |
| <kbd>Ctrl</kbd> + <kbd>S</kbd> | save |
| <kbd>Esc</kbd> | clear the selection |

**Validate** checks the graph without saving. **Save** validates first and
refuses to save until every error is fixed. Validation catches:

- loops;
- a step with two data inputs (allowed only when its *Run when* is *any*);
- an unknown condition field, an operator that does not fit the field
  (e.g. *starts with* on a number), a bad regex, a `between` without two
  values, a `query.<name>` with no matching probe;
- a probe whose filter is not a safe row expression;
- more than one *otherwise* link from a step;
- a step with no input or no output;
- two steps writing to the same output;
- a missing pipeline or address;
- a rollback-enabled step whose output storage cannot be rolled back;
- a bad trigger.

## Branching

Each link has an **outcome** and an optional **condition**. When a step
finishes, every link leaving it is either *taken* or *not taken*:

| Follow when | The link is taken when… |
| --- | --- |
| *the step succeeded* (default) | the step succeeded **and** its condition (if any) holds |
| *the step failed* | the step failed and its condition (if any) holds. This is a **recovery path**: the failure no longer stops the workflow |
| *always* | either way, if the condition holds |
| *otherwise* | the step succeeded and **none** of its other conditional links matched (the *else* branch) |

A step on a link that was not taken is **skipped**, and so is everything
behind it. Skipped steps write nothing, so there is nothing to roll back.
The run still ends `succeeded`.

### Where branches meet

A step with several incoming links has a **Run when** setting:

- **all incoming links are taken** (default). Every upstream step must
  finish and every link must be taken. If one branch was not taken, the
  step is skipped. Use it to wait for several pipelines before one final
  step.
- **any incoming link is taken.** The step waits until every upstream step
  has finished, then runs if at least one link was taken. Use it where an
  *if* / *else* re-joins. Such a step may have one data link per branch; it
  reads from the branch that ran.

### Conditions

A condition is one test or a group of tests. A group is **ALL of** (and)
or **ANY of** (or); groups nest, and **NOT** inverts a group. A test is
*field — operator — value*. It reads facts about the step that just
finished, never row values:

| Field | Type | Meaning |
| --- | --- | --- |
| `status` | text | `succeeded` / `failed` |
| `error` | text | the failed step's error line |
| `duration_s` | number | seconds the step's job ran |
| `pipeline`, `input_address`, `output_address` | text | what the step ran |
| `batch_id`, `root_input`, `trigger` | text | the run's batch (the watched folder path), input and trigger |
| `tables_completed`, `tables_failed` | list | tables the step finished / failed |
| `tables_completed_count`, `tables_failed_count` | number | how many |
| `table.<T>.status` | text | one table's status (`completed`, `failed`, …) |
| `warnings`, `errors`, `fixes_applied` | number | counts from the step's log |
| `health.blockers`, `health.checks_failed`, `health.checks_passed`, `health.checks_total`, `health.pass_rate` | number | from the step's health report |
| `health.failed_by_severity.<sev>` | number | failed checks of `blocker` / `warning` / `info` |
| `health.check.<id>.status` | text | one check's status |
| `health.failed_checks` | list | ids of the failed checks |
| `health.available` | true/false | the step wrote a health report |
| `output.tables`, `output.table_count` | list / number | tables now at the step's output |
| `rows.<T>` | number | rows in a table, when the step ran an analysis profile |
| `rows_written_total` | number | rows written, when the engine reports it |
| `query.<probe>` | number | a [data probe](#data-probes) on the step |

The operators depend on the field's type:

- **Numbers:** `=`, `≠`, `>`, `≥`, `<`, `≤`, *between*, *is one of*, *is
  not one of*.
- **Text:** `=`, `≠`, *contains*, *does not contain*, *starts with*, *ends
  with*, *matches regex*, *is one of*, *is empty*. Text tests ignore case
  unless **Aa** is ticked.
- **Lists:** *contains*, *does not contain*, *contains any of*, *contains
  all of*, *starts with* / *ends with* / *matches* (any item), *is empty*.
- **Any field:** *is known* / *is unknown*.

A fact that is not available makes a positive test false and a negative
test true. For example, with no health report `health.blockers = 0` is
false and `health.blockers is unknown` is true.

### Data probes

When a condition must look at the data itself, add a **data probe** to the
upstream step (step inspector → *Data probes*). A probe is a number
computed on the step's output after it succeeds:

- *row count* of a table, optionally **where** a row filter holds
  (`SEX = 'F'`, `ADMIT_DATE >= '2026-01-01'`);
- *distinct values*, *empty values* or *filled values* of one column.

Links leaving the step test it as `query.<name>`, e.g.
`query.female_patients > 0`.

- Probes run as a short `workflow_probe` job in the executor, with the
  step's status *probing*. Only the number comes back; no row values are
  shown.
- The filter is checked like a [`filter_expr`](../06-authoring/transforms.md)
  transform: one row expression from the allowed functions, with no
  sub-queries or aggregates.
- A probe that fails (an unknown table or column) fails the step, so a
  broken condition never quietly sends the run down the wrong branch.
- An agent (the assistant or MCP) that adds a probe with a filter needs a
  human `workflow.data_probe` [approval](../07-assistant/approvals.md),
  because a count under an arbitrary filter could reveal values one guess
  at a time.

## Triggers

A workflow owns its trigger. A new workflow starts with the trigger of its
first pipeline; change it in the inspector.

| Trigger | Starts a run |
| --- | --- |
| `manual` | only from ▶ Run, the API or the assistant |
| `cron` | on a crontab schedule, e.g. `0 2 * * *` |
| `interval` | every *N* minutes |
| `watch` | once per **new folder** under the first step's input; that step reads exactly that folder, and the data links carry it down the chain |

Points to note:

- **Pipelines keep their own triggers.** Running a pipeline on its own
  never starts a workflow that contains it.
- **A watch workflow needs exactly one first step**, whose input is a
  storage account that can be listed.
- **One run at a time.** A workflow has at most one active run. A new
  scheduled run, or a new watched batch, waits until the current run has
  finished.
- *Settings → Worker → route dispatch* pauses workflow triggers as well as
  pipeline triggers. See [Scheduling](../04-running-jobs/scheduling.md).

## Runs, failures and rollback

Every step runs as an ordinary [job](jobs.md), so it has its own run page.
The run view (`/workflows/<name>/runs/<id>`) draws the graph with live step
states and lists each step with its job, addresses, error and rollback
action. Links that were not taken are faded. Each step lists its branches
with ✓ / ✕ and the reason, for example
`health.blockers = 0 → false (health.blockers = 3)`. A skipped step says
which link was not taken.

**Before a step runs**, the worker looks at its output:

- **Occupied, and the step may not overwrite** (*Overwrite existing data*
  → *Refuse*, or the pipeline's own setting) — the step fails before
  anything is written.
- **Occupied, and rollback is on** — the existing tables are backed up
  first:
  - folder and object stores: a hidden `.ovid2_wf_backup/<run>/` folder
    inside the address;
  - Snowflake: a zero-copy clone into a sibling `<SCHEMA>_OVID2BK_<run>`
    schema.

**When a step fails with a taken *on failure* link**, the failure is
*handled*: that step's own output is rolled back (its status becomes
`rolled_back`), its recovery path runs, and the rest of the workflow
carries on. The run can still end `succeeded`; the run view counts the
handled failures.

**When a step fails with no failure path, or you cancel**, the remaining
steps are skipped, running jobs are cancelled, and every finished step is
**rolled back automatically**, newest first:

1. tables the run *created* are dropped;
2. tables the run *overwrote* are restored from the backup;
3. the backup is removed.

| Run status | Meaning |
| --- | --- |
| `running` | steps are queued or running |
| `succeeded` | every step finished, was skipped on a branch not taken, or failed into a recovery path; backups were removed |
| `rolled_back` | a step failed and every change was undone |
| `cancelled` | you cancelled; every change was undone |
| `rollback_failed` | undoing a step failed. The backups are kept; fix the cause (permissions, connectivity), then press **Retry rollback** |

Rollback is supported on `disc`, `s3`, `azure_blob` and `snowflake`
outputs. For a step that writes to any other storage type, turn **Roll
back if the workflow fails** off.

## Creating workflows with the assistant

Ask the [assistant](../07-assistant/assistant-tools.md), for example *"every
night run site_x ETL, then copy the result to the warehouse, then run the
DC checks"*. It reads your pipeline configs and addresses, then **drafts**
the workflow:

- the draft is saved **disabled** and opened on the canvas;
- you review it, adjust it, then **Save** and enable or **Run** it.

Nothing runs until you do. Agent Ovid over [MCP](../07-assistant/mcp.md)
has `create_workflow`, `update_workflow`, `run_workflow` and related tools.
An agent that grants a step *Overwrite existing data* needs a human
`run.overwrite` approval.

## Where workflows live

Definitions are JSON files under `$XDG_CONFIG_HOME/ovid2/workflows/`
(override with `OVID2_WORKFLOWS_DIR`), validated against
`resources/schemas/workflow.v1.json`. Runtime state and run history are
stored in `scheduler.db`. The REST API is under `/api/v1/workflows` and
`/api/v1/workflow-runs`; the condition catalogue (fields, operators) is at
`GET /api/v1/workflow-conditions`. See the
[API reference](../10-reference/api.md).
