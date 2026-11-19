---
title: Health & EDC checks
summary: Run the PCORnet EDC data-characterisation checks and Ovid2's health panels against any PCORnet database.
audience: [user, researcher]
---

The `health` kind runs two families of declarative checks:

- **PCORnet EDC / Data Characterization** checks (`DC 1.01` … `DC 4.03`);
- **Ovid2 health panels**: primary-key uniqueness, valueset conformance,
  null rate, future dates, freshness, mapping coverage, and orphan foreign
  keys.

It writes a durable `dcq_results` table and a browsable report.
`dq_checks` runs the same engine but selects checks by report cycle only.

## Run against any PCORnet database

You don't need one pipeline per site. Use the generic label
**`pcornet_edc`**: it fixes the CDM to `pcornet_7_0` and imports no site
package. Then choose the database at run time.

### Web

Open **Submit**, choose **Health**, and pick `pcornet_edc` under *Reusable
EDC / health labels*. For the source, choose a storage account, then the
database and the schema. Then choose the output.

### CLI

```bash
ovid2 health --pipeline pcornet_edc \
  --input_connection warehouse  --input_location  "CHAR_DEMO/PUBLIC" \
  --output_connection warehouse --output_location "CHAR_DEMO_QA/PUBLIC" \
  --packages 20r1
```

### API

```json
POST /api/v1/runs
{"kind":"health","pipeline":"pcornet_edc",
 "input_connection":"warehouse","input_location":"CHAR_DEMO/PUBLIC",
 "output_connection":"warehouse","output_location":"CHAR_DEMO_QA/PUBLIC",
 "packages":["20r1"]}
```

### MCP

```text
submit_health(input_connection="warehouse", input_location="CHAR_DEMO/PUBLIC", …, packages=["20r1"])
```

> [!WARNING]
> A warehouse account has no runnable root, so always pick a database and
> schema. Submitting at the account root is rejected with *"Choose a
> database/schema … a warehouse connection has no runnable root."*

## Choosing which checks run

Precedence: **`checks` › `packages` › `parts`**. A higher selector
silences the lower ones.

| Selector | Example | Meaning |
| --- | --- | --- |
| `--checks` | `1.01 DC_3_09` | exactly these checks |
| `--packages` | `20r1` | the union of a report cycle's checks |
| `--parts` | `part1 part3`, `mapping`, `integrity` | a section of the catalogue |

`ovid2 health --list-packages` lists the report cycles, newest first. The
newest cycle is the default.

## Report cycles

A cycle is a YAML manifest, `resources/health/packages/<cycle>.yaml`,
that lists check ids. Cycles share checks, so next quarter's cycle is a
new manifest and needs no code change. See
[Validate & test](../06-authoring/validate-and-test.md#add-a-report-cycle).

## Freshness and trends

Pass `--prev_input_address` (the previous refresh) to enable the freshness
and volume-trend panel.

## Any engine

Checks run unchanged on `pyspark`, `databricks_spark`, `snowpark` and
`duckdb`, against any storage the engine can reach.

### Snowpark scratch schema

On `snowpark`, a run needs somewhere it may create temporary views and
transient staging tables. Before the first check runs, Ovid2 picks a
writable **scratch schema** and binds the session to it (`USE DATABASE` /
`USE SCHEMA`). It takes the first of these that works:

1. **`OVID2_SNOWPARK_STAGING_SCHEMA`**, if set:
   - `<db>.<schema>` is used exactly as given;
   - a bare `<schema>` is paired with the session's current database or,
     if there isn't one, the first fallback database (see 3).
2. **`<current database>.OVID2_SNOWPARK_STAGING`**: the session's own
   database, from the Snowpark engine's `database` field. The schema is
   created if it doesn't exist (`CREATE SCHEMA IF NOT EXISTS`), so your
   role needs `CREATE SCHEMA` there. A failure here is reported, not
   skipped.
3. **`<fallback>.OVID2_SNOWPARK_STAGING`**, used only when the session
   has no database: the databases of the run's warehouse **output**
   address, then its **input** address. A database where `CREATE SCHEMA`
   fails is skipped with a warning, and the next one is tried.

The shared, read-only `SNOWFLAKE` system database never counts as
writable. If nothing works, the run stops before any check with an error
that lists the databases it tried.

> [!TIP]
> If the Snowpark engine names no `database`, scratch objects land in the
> database you write `dcq_results` to. To keep them somewhere else, export
> `OVID2_SNOWPARK_STAGING_SCHEMA=<db>.<schema>` for a schema your role
> can write to. Only scratch objects go there, never results.

The schema is kept between runs, so parallel runs don't race to create
it. Scratch objects inside it are named per run: staging tables start
with `OVID2_STG_` and checkpoints with `OVID2_CKPT_`. Staging objects are
dropped at the end of the run on a best-effort basis. If a run was killed,
find leftovers with `SHOW TABLES LIKE 'OVID2_STG_%'` in that schema. Set
`OVID2_SNOWPARK_STAGING_KEEP=1` to keep staging objects for debugging.

Reading the output: [Reading a health report](../05-data/reading-health-reports.md).
