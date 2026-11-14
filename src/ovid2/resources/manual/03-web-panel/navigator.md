---
title: Pipeline navigator
summary: Browse every pipeline by type down to each field and the chain of functions applied to it, open the YAML behind any step, and apply your own UDFs.
audience: [user, developer, researcher]
---

**Operate → Navigator** (`/navigator`) answers *"what happens to this
column, and where is that defined?"* for every installed pipeline package.
It reads the package's specs; it never runs the pipeline or reads data.

## The tree

The left-hand tree has five levels. Each level folds open and closed:

1. **Type** — packages grouped by the axis you choose in **Group by**:
   target CDM, ETL profile (`default` / `legacy_ovid`), run kind (from the
   saved pipeline configs that use the package), template, or CDM · profile.
2. **Pipeline** — one package. `ro` marks a generated legacy-parity port
   (read-only); `ƒ` marks a package with transform assignments.
3. **Table** — every table of the package's CDM, with five stage dots
   (format · deduplicate · transform · map · fix). A hollow dot means that
   stage is Python the navigator cannot see into. Grey tables are not
   configured (skipped by `--tables all`).
4. **Field** — every CDM column, flagged `key` (composite key),
   `constant` (always a literal), `unmapped` (no projection) or `extra`
   (produced by a step but not a CDM column).
5. **Step** — the ordered functions applied to that field.

You can narrow the tree in four ways:

- The **filter** box matches pipeline, table, field and function names.
  Press **Enter** to search packages you have not opened yet.
- **Show** keeps only fields with a UDF, editable packages, opaque Python
  steps, or unmapped / constant fields.
- The **stage** checkboxes hide whole stages, both in the tree and in the
  lanes.
- Arrow keys move through the tree (→ opens, ← closes).

The navigator remembers what you had open and how you grouped it. The
address bar is a deep link: `/navigator?p=<package>&t=<table>&f=<FIELD>`.

## The graph

| View | Shows |
| --- | --- |
| **Overview** (package) | A tree of the package's tables, each with its stage strip, field count and assigned chains. Click a table to open it. |
| **Lanes** (table) | One row per field and one column per stage. Each **chip** is a function applied to that field. Italic chips only *read* the field. A `⊘` chip also drops rows. A dashed chip is opaque Python. A hatched chip is read-only. Bands across the top are table-wide steps (deduplicate, joins, row filters). Hover a chip to draw lines from the columns it reads. |
| **YAML** | The selection's lineage as YAML (package summary, table, or one field's chain). |

## Editing a step

Click a chip and two panels open:

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

## For agents

`inspect_pipeline_lineage` (MCP and the assistant) returns the same
lineage as JSON. It is read-only, and it is the quickest way for an agent
to explain why a field holds the value it does.
