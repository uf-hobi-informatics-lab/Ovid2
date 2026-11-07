---
title: Storage accounts & warehouses
summary: Create and test connections, see the addresses built on them, and configure compute engines.
audience: [user, admin]
---

## Storage accounts

**Configure → Storage accounts** (`/storage`) lists every connection as a
card: type mark, name, endpoint summary, and **Test**, **Edit** and
**Delete** actions.

### Add a storage account

1. Click **+ New storage account** and pick a type.
2. Fill in the endpoint fields. They depend on the type:

   | Type | Typical fields |
   | --- | --- |
   | disc | `host_path` (folder on the host), optional `container_path`, `mount_mode` (`rw` / `ro`) |
   | s3 | `region`, optional `bucket_default`, endpoint for S3-compatible stores |
   | azure_blob | `account_name`, `container_default`, `endpoint_suffix` |
   | gcs | bucket, project |
   | snowflake | `account`, `user`, `role`, `warehouse`, key-pair file references |
   | databricks | host, HTTP path, catalog |
   | postgres / mssql | host, port, database, user |
   | access | `host_path` (folder holding `.accdb` / `.mdb` files) |

3. Fill in the **secret** fields (keys, passwords, tokens). They are
   written to `secrets.d/<name>.json`, mode 0600, and are never shown
   again.
4. Click **Test**. The test runs in a short-lived executor subprocess that
   sees only this secret.

> [!TIP]
> Use `mount_mode: ro` for disc folders that hold raw feeds. Jobs can then
> read them but never modify them.

### Addresses

Addresses are created automatically when you pick a location in the
Submit or Pipelines pages, and named `<connection>__<location>`. They are
listed under the storage account they belong to (`/addresses`). You can
also create one by hand: pick a connection, then a location.

Deleting a connection that addresses still use is refused.

## Warehouses (engines)

**Configure → Warehouses** (`/warehouses`) lists compute **engine**
configs. Each has a type (`pyspark`, `databricks_spark`, `snowpark` or
`duckdb`) and type-specific arguments:

| Type | Typical arguments |
| --- | --- |
| pyspark | Spark config overrides, e.g. `spark.executor.cores` |
| databricks_spark | workspace host, cluster id, secret ref |
| snowpark | `connection` (a Snowflake storage account) |
| duckdb | `threads`, `memory_limit`, `temp_directory`, `table_concurrency` |

**Test** probes the engine from an executor subprocess. Engine choice is
explained in [Engines](../02-concepts/engines.md).
