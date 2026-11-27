---
title: Pipelines
summary: Saved pipeline configs, schedules and open sides — plus the pipeline packages, formatters and mappers behind them.
audience: [user, developer]
---

**Operate → Pipelines** (`/pipelines`) has two parts: saved **pipeline
configs** (jobs you can run or schedule) and the installed **pipeline
packages** (the rules those jobs use).

## Saved pipeline configs

Configs are grouped by kind (ETL, Analysis, DQ checks, Health, Copy,
Move). Each row shows:

- the flow, *input → engine → output*;
- the trigger, and its last and next fire times;
- whether it is enabled;
- its actions: **run**, **edit**, **delete**.

When a config reads one data model and writes another, its card shows
both, for example `omop_5_3 → pcornet_7_0`.

You can filter by name, CDM, input CDM (*Any input*), type, trigger and
status.

### Input CDM

Every config has an **input CDM** (`input_cdm`): the data model it *reads*,
as opposed to the CDM it writes. If you don't set it, the config uses the
package's `INPUT_CDM` setting. If the package has none, the input CDM is the
same as the CDM. To change it, open the config's **edit** page and pick from
the *Input CDM* select. Choose *(auto)* to go back to the package's value.
The assistant uses this field to find a conversion pipeline, for example
OMOP → PCORnet.

### Create a pipeline config

Click **+ New pipeline**. The wizard has up to eight steps — the rail at
the top shows which are done or need attention, and any step can be opened
at any time. The right-hand flow graph (*Source → Format → Transform →
Map → Fix → Engine → Output*) redraws as you go.

1. **Type** — name and kind.
2. **What it runs** — for ETL, pick a template. Its **tables** are listed
   with, for each, the formatter (YAML adapter / custom class), the source
   files it expects, the mapper, the fix chain and assigned transforms.
   Keep *All configured* or *Pick tables* to save a subset.
3. **Identity** (ETL only) — every name the new package must match
   elsewhere (HARVEST network / datamart ids, warehouse databases, Datavant
   key), pre-filled with name-derived defaults, plus the
   cross-configuration checks (salt keys, dictionaries, fixer references,
   class paths).
4. **Source** — input storage. For ETL, each table's file pattern is
   matched against the chosen folder.
5. **Engine** — any configured engine; every engine runs every package.
6. **Destination**, 7. **Automation**, 8. **Review**.

What step 2 asks depends on the kind:

| Kind | The form shows |
| --- | --- |
| ETL | an **ETL template** picker. On save, a pipeline package is scaffolded under the config's name. |
| Analysis | a **Pipeline package** picker (existing packages only) |
| DQ checks | an **EDC report cycle** picker |
| Health / Copy / Move | no package picker; the name is only a label |

Then choose:

- the **input** and **output**: a bound storage location, or *Keep open —
  choose at run time*;
- the engine, workers, tables and stages;
- a **trigger**. See [Scheduling](../04-running-jobs/scheduling.md).

> [!TIP]
> A **watch** or **scheduled** config writes to one fixed output every
> time. Tick **Runs may overwrite existing output data**
> (`allow_overwrite`), or every run after the first stops at the overwrite
> gate.

### Run a config with an open side

An open row shows a storage-account picker for each open side, next to a
**run** button:

- Pick the account(s) and click **run** to submit immediately, at the
  account root.
- Leave a side unpicked (or click **browse…**) to fall back to the Submit
  page with this pipeline pre-selected. There you can browse to a
  sub-folder.

## Pipeline packages

**Packages** in the sidebar (`/pipeline-packages`) lists every installed
`ovid2_pipelines.*` package. The **Pipelines** page lists saved configs only, so a
package doesn't appear there until a config exists for it. To run a package with
the input picked per run, save a config with both sides left open. Open a package to see:

- its settings (CDM, mapper type, default database);
- its **formatters** and **mappers**, with their adapter, sources and table;
- its Python sources (read-only preview).

Click **Customize** (on the package, the pipeline config or the pipeline
row) to open the **pipeline builder** (`/pipeline-packages/<name>/builder`):

- the left tree lists package documents — *Name checks*, *Transform
  assignments*, *Private transforms*, *Fixer chains*, *Settings* — and
  every CDM table with four status dots (format · transform · map · fix);
- picking a table draws its flow; click a stage to edit it;
- every document has a **Form** tab (generated from its schema: transform
  ops, mapper ops, fixers with their named arguments, formatter adapter and
  projections, literal settings) and a **YAML** tab (CodeMirror,
  linted live). They stay in sync; saving from the form re-serialises the
  YAML and drops comments, the YAML tab keeps them.

Saves are validated on the server (schema, op parameters, the SQL
expression allow-list, the fixer registry, CDM tables) and audited.
Settings edits change only literal constants — computed values stay
read-only. Custom Python formatters are shown read-only.

### Open in navigator

Every place that shows a package links to the
[pipeline navigator](navigator.md), opened on that package
(`/navigator?p=<package>`):

| Where | Link |
| --- | --- |
| Pipeline builder | **Open in navigator** button in the header |
| Package page (`/pipeline-packages/<name>`) | **Open in navigator** button next to **Customize →** |
| Pipelines list | the navigator icon in the row's actions |
| Pipeline config page | **Navigator** button next to **Customize** |

### Delete a package

Deleting a pipeline config on **Pipelines** removes only the config. The
package stays installed. To remove the package itself, click **Delete
package** on the package page or in the navigator's package panel, then
confirm the dialog.

- The package folder is **moved**, not erased, to
  `$XDG_STATE_HOME/ovid2/trash/pipelines/<name>-<timestamp>/`. Restore it
  with `mv`.
- The run configs that use the package are deleted with it. The
  confirmation dialog lists them.
- The package disappears from the navigator and the package list
  immediately.
- The button is shown only to users with the manage-config capability.
  It is hidden for **template sources**: the packages every
  *+ New pipeline* clone starts from cannot be deleted.
- A package whose folder is a symlink is refused. Remove it by hand.

From the command line:

```bash
ovid2 pipeline delete demo                  # refused if a run config still uses it
ovid2 pipeline delete demo --with-configs   # also delete those run configs
ovid2 pipeline delete demo --with-configs --yes   # no confirmation prompt
```

Agents and scripts use `DELETE /api/v1/pipeline-packages/{name}`
(`?with_configs=true` to include the configs). It is
[approval-gated](../07-assistant/approvals.md) (`pipeline_package.delete`):
it answers `202` with an `approval_id` until a human approves. It answers
`409` while run configs still use the package and `with_configs` is not
set, and `400` for a template source or an unknown package.

> [!TIP]
> If git tracks the deleted folder, it shows up as an unstaged deletion.
> Commit or restore it separately.

## Transform library

**Transforms** in the sidebar (`/transforms`) is the reusable transform
library: chains ("UDFs") you create once and **assign** to tables of any
pipeline. The left tree lists **My transforms** (yours, stored in
`$XDG_CONFIG_HOME/ovid2/transforms/`) and **Shipped (read-only)** chains,
plus **All ops**, the op catalogue. The concepts are in
[Transforms library](../06-authoring/transforms.md).

Creating, saving and assigning need the manage-config capability.

### 1. Create a transform

1. Click **New transform** and enter a name: lower-case letters, digits
   and `_`, starting with a letter.
2. The editor opens with an example `derive_column` step. Build the
   chain on the **Form** tab (add, reorder and remove steps, fill in each
   op's parameters) or write it on the **YAML** tab. The two tabs stay
   in sync.
3. Click **Save**. The chain is validated on the server first: the
   schema, each op's parameters and the SQL allow-list.

To customise a shipped chain, open it and click **Duplicate…**, then save
the copy under a new name.

### 2. Test it on a sample

- **Check the expressions as you type.** For `derive_column` and
  `filter_expr`, the editor checks the expression against the allow-list
  of row functions and shows *✓ allowed row expression* or the reason it
  is refused. **Save** runs the full validation and lists any errors
  under the editor.
- **Run it on a small sample.** The library page does not run chains on
  data. Assign the chain to a scratch copy of your pipeline (step 3),
  then run that pipeline on a sample input with DuckDB, for example with
  `--transformations format deduplicate transform`. Inspect the result in
  the [Data Space](dataspace.md). See
  [Test on a sample](../06-authoring/validate-and-test.md#test-on-a-sample).

### 3. Assign it to a package table

With a saved chain open, use the **Assign to a pipeline** panel:

1. Pick the pipeline package.
2. Pick one or more of its CDM tables, or `* (every table)`.
3. Click **Assign**.

This appends the chain to the package's `transforms/assignments.yaml`.
The status line links to the pipeline builder, opened on *Transform
assignments*, where you can reorder or remove the assignment. The chain
runs in the package's `transform` stage, between deduplicate and map.
Shipped chains can be assigned as they are.

### 4. See where it is used

The same panel lists **Used by**: every *pipeline · table* the chain is
assigned to, and the fields it writes. Each entry opens the navigator on
that table. A chain that is not assigned anywhere shows *Not assigned to
any pipeline yet.*

### 5. Delete it

**Delete** (in the editor's action bar) removes one of your own chains.
Shipped chains cannot be deleted. Because assigned pipelines would fail
on their next run, deleting a library chain needs a **human approval**
(`transform.delete`). The first attempt shows *Awaiting human approval*
with an approval id. Once a system admin has approved it on the
[approvals](../07-assistant/approvals.md) API, click **Delete** again.

> [!TIP]
> Check **Used by** before you delete a chain, and remove its
> assignments first.

Authoring guide: [Authoring pipelines](../06-authoring/index.md).
