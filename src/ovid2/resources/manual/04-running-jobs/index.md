---
title: Running jobs
summary: Every job type, the three ways to start one, and how to follow it to completion.
audience: [user]
---

Every job, whatever started it, goes through the same queue and the same
executor. This chapter covers each job kind end to end.

| I want to… | Kind | Page |
| --- | --- | --- |
| turn raw extracts into a CDM | `etl` | [ETL runs](etl.md) |
| check data quality (EDC / health) | `health`, `dq_checks` | [Health & EDC checks](health-checks.md) |
| profile a source before mapping it | `analysis` | [Analysis & DQ](analysis-and-dq.md) |
| copy tables between systems unchanged | `copy`, `move` | [Copy & move](copy-and-move.md) |
| download a table as a file | `export` | [Exporting tables](export.md) |
| run something on a timer or on new data | any | [Scheduling](scheduling.md) |
| re-run into the same output | any | [Overwrite & batches](overwrite-and-batches.md) |
| watch progress, read logs, cancel | any | [Monitoring & control](monitoring.md) |

## Three ways to start a job

Every surface accepts the same fields: kind, pipeline, input, output,
tables, stages, engine and workers. Choose whichever suits you.

### Web

Use **Operate → Submit**, or click **run** on a saved pipeline. See
[Submit](../03-web-panel/submit.md).

### CLI

```bash
ovid2 etl --pipeline example_pcornet \
  --input_address inbox__2026_03 --output_address warehouse__char_demo_public \
  --batch_id inbox/2026_03 --tables all --transformations all --engine duckdb
```

### REST API

```bash
curl -s -X POST http://127.0.0.1:8090/api/v1/runs \
  -H 'content-type: application/json' \
  -d '{"kind":"etl","pipeline":"example_pcornet",
       "input_address":"inbox__2026_03","output_address":"warehouse__char_demo_public",
       "tables":["all"],"transformations":["all"],"engine":"duckdb"}'
```

The response carries `job_id` and `instance_id`. Follow the job at
`/runs/<instance_id>`.

> [!NOTE]
> When you start a job with the CLI, it runs directly on the host: it is
> not queued and doesn't need the worker. Web, API and MCP submissions are
> queued, and the worker runs them.
