---
title: Connections & addresses
summary: A connection is how to reach a storage system; an address is the exact place inside it.
audience: [user, developer]
---

## Connection — *how* to reach a system

A **connection** (shown as a *storage account* in the panel) holds the
details needed to reach a system:

- its type and endpoint (host, account, bucket, root folder…);
- a reference to its secret.

Many addresses reuse one connection. Connections live as JSON files in
`$XDG_CONFIG_HOME/ovid2/connections/<name>.json`:

```json
{
  "type": "s3",
  "args": { "region": "us-east-1", "bucket_default": "research-landing" },
  "secret_ref": "s3_landing"
}
```

The secret itself is in `secrets.d/s3_landing.json` (mode 0600). The web
panel never sends it back to the browser.

Supported types: `disc`, `s3`, `azure_blob`, `gcs`, `snowflake`,
`databricks`, `postgres`, `mssql`, `access`. See the
[driver table](../10-reference/drivers-engines.md) for which are native and
which are DuckDB-only.

## Address — *where* exactly

An **address** names a connection and a **location** inside it:

```json
{ "connection": "s3_landing", "location": { "path": "inbox/2026_03" } }
```

```json
{ "connection": "warehouse", "location": { "database": "CHAR_DEMO", "schema": "PUBLIC" } }
```

Addresses live in `$XDG_CONFIG_HOME/ovid2/configs/<name>.json`. The panel
creates them for you when you browse to a folder or schema. The panel
names them `<connection>__<location>`.

### An address is the exact location

> [!IMPORTANT]
> Ovid2 reads from, and writes to, **exactly** the address location.
> Nothing is appended to it: no `{pipeline}/{batch}` sub-folder, and no
> generated schema name.

| Store | Location | Data is read / written at |
| --- | --- | --- |
| disc | `path: /srv/inbox/2026_03` | `/srv/inbox/2026_03/<table>.csv` |
| S3 / Azure / GCS | `path: inbox/2026_03` | `s3://<bucket>/inbox/2026_03/<table>.<fmt>` |
| Snowflake / Databricks | `DATABASE/SCHEMA` | `DATABASE.SCHEMA.<TABLE>` |

This has three consequences:

- **The batch id is derived, not chosen.** It is the input's full Data
  Space path (`<account>/<path>`). It labels the run and is never used to
  build a path.
- **Re-running replaces output.** If the output location already holds
  tables, Ovid2 asks for confirmation first. See
  [Overwrite & batches](../04-running-jobs/overwrite-and-batches.md).
- **Warehouse outputs must name a schema.** A Snowflake or Databricks
  output that names only a database is rejected at submit time.

## Legacy address shape

Older addresses use `{"src_name": "<type>", "src_args": {…}}`, with
endpoint and location mixed together. They still load.
`ovid2 tools migrate-addresses` converts them to the connection shape.

## Open sides

A saved pipeline config may leave its input, its output or both **open**.
You then choose the storage at run time, from the Pipelines page or the
Submit page. Open inputs cannot use a `watch` trigger, because there is
nothing to watch.
