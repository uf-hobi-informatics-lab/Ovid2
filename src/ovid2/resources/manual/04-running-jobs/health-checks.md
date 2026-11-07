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

Reading the output: [Reading a health report](../05-data/reading-health-reports.md).
