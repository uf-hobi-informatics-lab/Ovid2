# Data Space explorer

The **Data Space** page (`/dataspace`, nav: *Explore → Data Space*) is a
navigable view of every storage account the signed-in user may see
(capability `view_pipeline_data`).

## Navigating

- **Left pane — the tree.** Storage accounts grouped by storage type
  (*By type*, collapsible groups with a count) or flat (*A–Z*). The
  sort button flips A→Z / Z→A; ⊞ / ⊟ expand / collapse every group; the
  filter box narrows accounts and already-loaded folders. Accounts,
  folders, databases and schemas expand lazily. Icons are the same
  storage-type marks as the Submit page. Grouping, sort and open groups
  are remembered per browser.
- **Right pane.** The root shows storage-type and account cards; an
  account / folder / schema shows a sortable listing (name, kind, size,
  rows, last modified) with a filter; a table / data file opens the
  **table detail**.
- The URL follows the selection (`/dataspace/<account>/<path…>`), so
  every view can be bookmarked or shared and the Back button works.

## Table detail

| Stat | Source (never a data scan) |
| --- | --- |
| Rows | warehouse statistics / `INFORMATION_SCHEMA`, parquet footers, CSV estimate from the first MiB (*estimate* badge) — **Count rows** runs an exact count on demand |
| Fields | `INFORMATION_SCHEMA.COLUMNS` / JDBC metadata, parquet schema, CSV header (types inferred from the first 200 rows) |
| Size, last modified, created | `SHOW TABLES`, catalogue views, object metadata, file `stat` |
| Part files, delimiter | Spark-written `<table>.csv/` directories; detected delimiter |

Tabs: **Fields**, **Sample data** (25 / 100 / 500 rows), and for databases
**Definition** (Snowflake `GET_DDL`, Databricks `SHOW CREATE TABLE`,
Postgres / SQL Server / Access reconstructed from the catalogue).

## Export

*Export…* opens a dialog:

- **Format** — CSV, Excel (`.xlsx`), PDF, Word (`.docx`).
- **CSV delimiter** — comma, semicolon, tab, pipe or any single character.
- **Header case** — as stored, `UPPER`, `lower` (every format).
- **Rows** — the first *N* rows, capped per format for an immediate
  download: CSV / Excel `$OVID2_EXPORT_MAX_ROWS` (default 100 000),
  PDF / Word `$OVID2_EXPORT_DOC_MAX_ROWS` (default 5 000).
- **Whole table as a background job** (CSV / Excel, needs
  `submit_jobs`) — queues an `export` job; its run page shows
  **Download export** when done.

Every export is audited (`dataspace.export` / `dataspace.export_job`).
Files are written mode 0600 under `$XDG_STATE_HOME/ovid2/web_exports/`
(override `$OVID2_WEB_EXPORT_DIR`) and quick-download files are pruned
after one hour.

## Store support

| Type | Browse | Detail / sample / export | Install |
| --- | --- | --- | --- |
| disc | ✓ | csv / tsv / txt (+ .gz), parquet, json-lines, part directories | base |
| s3 | ✓ | as disc, ranged `GetObject` | `ovid2[s3]` |
| azure_blob | ✓ | as disc, ranged downloads | `ovid2[azure]` |
| gcs | ✓ | as disc, ranged downloads | `ovid2[gcs]` |
| snowflake | ✓ | ✓ + `GET_DDL` | base |
| postgres | ✓ | ✓ (`reltuples` estimate) | `ovid2[postgres]` |
| mssql | ✓ | ✓ (`TOP n`, partition-stat counts) | `ovid2[mssql]` |
| databricks | ✓ | ✓ + `SHOW CREATE TABLE` | `ovid2[databricks]` |
| access | ✓ | ✓ (UCanAccess, no Spark) | `ovid2[access]` + jars |

PDF / Word export needs `ovid2[export]` (reportlab, python-docx) on the
host that runs the web tier's executor subprocesses. A missing SDK
shows *not available* instead of an error.

## API

| Endpoint | Capability |
| --- | --- |
| `GET /api/v1/dataspace?path=` (listing; now with `size_bytes`, `modified_at`, `row_count`) | `view_pipeline_data` |
| `GET /api/v1/dataspace/table?path=[&refresh=true]` | `view_pipeline_data` |
| `GET /api/v1/dataspace/table/sample?path=&limit=` | `view_pipeline_data` |
| `GET /api/v1/dataspace/table/count?path=` | `view_pipeline_data` |
| `GET /api/v1/dataspace/table/export?path=&format=&delimiter=&header_case=&limit=` | `view_pipeline_data` |
| `POST /api/v1/dataspace/table/export-jobs` | `submit_jobs` |
| `GET /api/v1/jobs/{job_id}/artifact` | `view_pipeline_data` |

Credential-bearing work runs in short-lived `ovid2 tools describe-table
| sample-table | count-rows | export-table` executor subprocesses —
the API process never holds a credential. The assistant may call only
the metadata endpoint (`ods_describe_table`); MCP also has
`ods_sample_table`.
