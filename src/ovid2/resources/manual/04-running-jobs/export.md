---
title: Exporting tables
summary: Download any table Ovid2 can see as CSV, Excel, PDF or Word — instantly for samples, as a job for whole tables.
audience: [user, researcher]
---

## From the Data Space

Open a table in **Explore → Data Space** and click **Export…**.

| Choice | Result |
| --- | --- |
| first *N* rows, *N* ≤ 100 000 (CSV / Excel) or ≤ 5 000 (PDF / Word) | an immediate download |
| **Whole table as a background job** (CSV / Excel) | an `export` job; its run page offers **Download export** when done |

Other options: the CSV delimiter (any single character) and the header
case (as stored, `UPPER` or `lower`).

## From the CLI

```bash
# <connection> --path <listing path under that storage account>
ovid2 tools export-table warehouse --path CHAR_DEMO/PUBLIC/DEMOGRAPHIC \
  --format csv --delimiter '|' --header-case upper --out demographic.csv
ovid2 tools export-table inbox --path landing/2026_03/person.csv --format xlsx --limit 5000 --out person.xlsx
```

Without `--limit` (or above the host row limit) the export runs in a
container; `--in-process` forces a host-side run. Run
`ovid2 tools export-table --help`, or see the
[CLI reference](../10-reference/cli.md#ovid2-tools-export-table), for every
flag.

## How exports are protected

- An export with no row limit, or one above `OVID2_EXPORT_MAX_ROWS`, runs
  in a **driver container** that holds only the source connection's
  secret. Full-table rows never pass through a host process.
- Files are written with mode 0600 under
  `$XDG_STATE_HOME/ovid2/web_exports/` (override with
  `OVID2_WEB_EXPORT_DIR`). Quick downloads are removed after one hour.
- Every export is audited (`dataspace.export`, `dataspace.export_job`).
- **Agents** (MCP, the assistant) need a single-use human approval for
  each table before they can export or sample rows.

> [!NOTE]
> PDF and Word export need the `export` extra (`reportlab`,
> `python-docx`) on the host that runs the panel's executor subprocesses.
> Without it the format shows as *not available*.
