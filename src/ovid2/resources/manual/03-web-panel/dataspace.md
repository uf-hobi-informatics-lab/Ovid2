---
title: Data Space explorer
summary: Browse every storage account as folders, inspect any table's fields and sample rows, and export it.
audience: [user, researcher]
---

**Explore → Data Space** (`/dataspace`) needs `view_pipeline_data`
(pipeline operator or above). The concept is explained in
[The Data Space](../02-concepts/dataspace.md).

## Navigating

- **Left: the tree.** Storage accounts, either grouped by storage type
  (*By type*) or flat (*A–Z*). Use the sort button to flip the order,
  ⊞ / ⊟ to expand or collapse every group, and the filter box to narrow
  the list. Folders, databases and schemas load as you expand them.
- **Right: the view.** The root shows account cards. A folder or schema
  shows a sortable listing (name, kind, size, rows, last modified). A table
  or data file opens the **table detail**.
- **The URL follows the selection** (`/dataspace/<account>/<path…>`), so
  you can bookmark or share any view, and Back works.

## Table detail

| Shows | Comes from (never a full scan) |
| --- | --- |
| Rows | warehouse statistics, Parquet footers, or a CSV estimate from the first MiB (marked *estimate*). Click **Count rows** for an exact count. |
| Fields | `INFORMATION_SCHEMA`, JDBC metadata, the Parquet schema, or the CSV header with types inferred from the first 200 rows |
| Size, modified, created | catalogue views, object metadata, file `stat` |
| Part files, delimiter | Spark `<table>.csv/` directories, delimiter sniffing |

Tabs:

- **Fields**.
- **Sample data** (25, 100 or 500 rows).
- **Definition**, for databases. This is the DDL: Snowflake `GET_DDL`,
  Databricks `SHOW CREATE TABLE`, or a reconstruction for Postgres, SQL
  Server and Access.

## What each store supports

Every storage type can be browsed, described, counted, sampled and
exported. Some types need an optional extra installed on the host that
runs the web panel. Without it, the page shows *not available* instead of
an error.

| Storage type | Browse | Fields / rows | Count rows | Sample data | Export | Definition tab | Install |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `disc` | ✓ | ✓ csv / tsv / txt (+ `.gz`), Parquet, JSON lines, part directories | ✓ | ✓ | ✓ | — | base |
| `s3` | ✓ | ✓ as disc (ranged reads) | ✓ | ✓ | ✓ | — | `ovid2[s3]` |
| `azure_blob` | ✓ | ✓ as disc (ranged reads) | ✓ | ✓ | ✓ | — | `ovid2[azure]` |
| `gcs` | ✓ | ✓ as disc (ranged reads) | ✓ | ✓ | ✓ | — | `ovid2[gcs]` |
| `snowflake` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ `GET_DDL` | base |
| `postgres` | ✓ | ✓ (row estimate from statistics) | ✓ | ✓ | ✓ | ✓ reconstructed | `ovid2[postgres]` |
| `mssql` | ✓ | ✓ (partition-statistics counts) | ✓ | ✓ | ✓ | ✓ reconstructed | `ovid2[mssql]` |
| `databricks` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ `SHOW CREATE TABLE` | `ovid2[databricks]` |
| `access` | ✓ | ✓ (no Spark needed) | ✓ | ✓ | ✓ | ✓ reconstructed | `ovid2[access]` plus the UCanAccess jars |

Export to CSV and Excel works with the base install. PDF and Word export
also need `ovid2[export]`. For example:

```bash
pip install 'ovid2[s3,export]'
```

## Export

**Export…** opens a dialog:

| Field | Options |
| --- | --- |
| Format | CSV, Excel (`.xlsx`), PDF, Word (`.docx`) |
| CSV delimiter | comma, semicolon, tab, pipe, or any single character |
| Header case | as stored, `UPPER`, `lower` |
| Rows | the first *N* rows. Immediate downloads are capped at 100 000 (CSV / Excel) or 5 000 (PDF / Word). |
| Whole table as a background job | CSV / Excel; queues an `export` job |

A whole-table export runs in a **driver container** that holds only the
source connection's secret, so full-table row data never passes through a
host process. When the job finishes, its run page shows **Download
export**. See [Exporting tables](../04-running-jobs/export.md).

Every export is audited. Files are written with mode 0600, and
quick-download files are deleted after one hour.
