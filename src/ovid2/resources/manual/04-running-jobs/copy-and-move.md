---
title: Copy & move
summary: Stream tables between any two storage locations unchanged — explicit lists or "all", largest first.
audience: [user]
---

`copy` and `move` skip the format → map → fix chain and stream tables from
a source address to a destination address. `move` is the driver-native
successor to `copy`; both leave the source in place.

```bash
ovid2 tools copy --from inbox__2026_03 --to warehouse__sandbox_public \
  --pipeline demo --batch_id inbox/2026_03 --tables all --overwrite
```

## Choosing tables

Each `--tables` entry is either `LOGICAL` or `LOGICAL=SOURCE`
(`demographic=DEMOGRAPHIC_2026.csv`). Or pass **`all`** to copy everything
the source holds:

| Source | `all` means |
| --- | --- |
| snowflake | every table and view in `DATABASE.SCHEMA` |
| disc | every data file, or Spark `<t>.csv/` directory, directly in the folder |
| s3, azure_blob | every object or `<t>.csv/` prefix directly under the path |
| access | every user table in the `.accdb` / `.mdb` |

Junk entries are skipped: `_SUCCESS`, `.crc`, hidden files, partitions and
plain sub-folders. File sources produce extension-free table names.
`postgres`, `mssql`, `gcs` and `databricks` still need an explicit list.

When you submit `all` through the web, the API or MCP, it is **expanded
before the job is stored**, so the job record and any draft you review
list the real tables.

## Largest table first

`copy` runs tables in parallel (`OVID2_COPY_TABLE_CONCURRENCY`, default
8), starting with the largest source tables. Sizes come from metadata, not
scans. Total time is then close to the time of the single largest table.
Set `OVID2_COPY_ORDER=as_given` to keep your order.

## Snowflake → Snowflake without leaving Snowflake

When the job's engine is `snowpark`, each table is copied server-side
(`session.table(src).save_as_table(dst)`), and the host runs one small
container with no Spark cluster. Set up:

1. an engine config `{"type": "snowpark", "args": {"connection": "warehouse"}}`;
2. a saved copy config using that engine, with open input and output.

The destination is the exact `DATABASE/SCHEMA` you name, created if
missing. A database-only destination is rejected.

## Access databases

An `access` connection points at a **folder**, and an address picks one
file in it. The Data Space can list a file's tables when the host has the
`access` extra and the UCanAccess jars (`OVID2_UCANACCESS_JARS`).
Otherwise browsing stops at the file. `--tables all` still works, because
it enumerates inside the container.
