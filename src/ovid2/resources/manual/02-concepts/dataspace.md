---
title: The Data Space
summary: Every storage account, of any type, presented as one tree of plain folders.
audience: [user, researcher, developer]
---

The **Data Space** (ODS) shows every storage account Ovid2 knows about —
disc, object store or warehouse — as **one tree of plain directories**:

```text
/                                   all accounts
/<account>/                         one storage account (connection)
/<account>/<folder>/<folder>/…      disc / S3 / Azure / GCS folders
/<account>/<DATABASE>/<SCHEMA>/     warehouse database and schema
/<account>/…/<table>                a table or data file
```

The model is the same whatever the storage type:

- **Every location has one path.** The full path of a folder is the
  **batch id** of any run that reads it, so `inbox/landing/2026_03` both
  identifies the data and labels the run.
- **Addresses are views onto that tree.** When you pick a folder in the
  Submit page, Ovid2 resolves or creates the address for exactly that path.
- **Browsing is metadata-only.** Names, sizes, row estimates, columns and
  DDL come from catalogues, object listings and file headers. Nothing is
  scanned.

## Ways to explore it

| Surface | How |
| --- | --- |
| Web | **Explore → Data Space**. See [Data Space explorer](../03-web-panel/dataspace.md). |
| CLI | `ovid2 tools ods ls /<account>/<path>`, `ods resolve`, `ods dimensions` |
| API | `GET /api/v1/dataspace?path=/<account>/<path>` |
| MCP | `ods_list`, `ods_resolve`, `ods_describe_table` |

## Row values are special

Listing, describing and counting are open to everyone who has
`view_pipeline_data`. **Sampling or exporting rows** runs in an executor
subprocess that sees only that connection's secret. When an *agent* asks,
it also needs a single-use human approval for that one table. See
[Approvals](../07-assistant/approvals.md).
