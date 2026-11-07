---
title: Glossary
summary: Every Ovid2 term in one place, with a link to where it is explained in depth.
audience: [user, researcher, developer, admin]
---

Terms are alphabetical. Each entry links to the page that explains it.

## A–C

Adapter
: The framework strategy a [formatter](#formatter) uses to build a table:
  `source_passthrough`, `semantic_join`, `omop_concept_join`,
  `union_of_specs` or `custom`. → [Formatter specs](../06-authoring/formatters.md)

Address
: *Where* data is read from or written to: a [connection](#connection)
  plus a location (folder path or `DATABASE/SCHEMA`). An address is the
  **exact** location — nothing is ever appended to it.
  → [Storage](../02-concepts/storage.md)

Agent Ovid
: The AI planner — the MCP server (`ovid2 mcp`) and the in-panel
  assistant. It can read through the API and propose actions, never act on
  data directly. → [Assistant & agents](../07-assistant/index.md)

Approval
: A human sign-off that a gated operation (a destructive change, an agent
  reading row values, an agent-requested overwrite) needs before it runs.
  → [Approvals](../07-assistant/approvals.md)

Batch id
: The label of one run's input: the input location's complete Data Space
  path, `<account>/<path>`. A label only — never used to build a path.
  → [Overwrite & batches](../04-running-jobs/overwrite-and-batches.md)

CDM
: Common Data Model — the target table shapes, keys and allowed values
  (`pcornet_7_0`, `omop_5_3`). → [Data models](../02-concepts/cdms.md)

Composite key
: The columns that identify a row in a CDM table, used to de-duplicate
  (e.g. `PATID` for `DEMOGRAPHIC`).

Connection
: *How* to reach a storage system: type, endpoint, and a reference to its
  secret. Reused by many addresses. → [Storage](../02-concepts/storage.md)

Contract test
: A test under `tests/contract/` that enforces a repository-wide
  invariant. → [Invariants](../09-developers/invariants.md)

## D–F

Data Space (ODS)
: Every storage account presented as a tree of folders,
  `/<account>/<path…>`. → [Data Space](../02-concepts/dataspace.md)

DC / EDC check
: A PCORnet Data Characterization check (e.g. *DC 1.01*), run by the
  `health` and `dq_checks` job kinds.
  → [Health checks](../04-running-jobs/health-checks.md)

Dictionary
: A Python mapping of source values to standard values, defined in a
  pipeline's `dictionaries.py` and used by `translate_by_dict`.

Draft job
: A fully specified job that waits, unqueued, until a human presses ▶.
  The assistant hands work to you this way.

Driver
: The code that talks to one storage type (`disc`, `s3`, `snowflake`, …).
  → [Drivers & engines](../10-reference/drivers-engines.md)

Engine
: The compute backend: `pyspark`, `databricks_spark`, `snowpark` or
  `duckdb`. → [Engines](../02-concepts/engines.md)

Event log
: The append-only `events-<instance_id>.jsonl` file every run writes; the
  live views read it. → [Isolation & events](../02-concepts/isolation-and-events.md)

Fixer
: A reusable data-correction step (e.g. `future_date_to_null`) a pipeline
  opts into per table. → [Fixer chains](../06-authoring/fixers.md)

Flavour of null
: The set of values a CDM treats as "no information" (`NI`, `UN`, `OT`, …).

Formatter
: A spec that builds one CDM table from raw sources.
  → [Formatter specs](../06-authoring/formatters.md)

## H–M

Health check
: A declarative data-quality check — an EDC DC query or an Ovid2-native
  panel (freshness, null rate, orphan keys, …).

Instance id
: `YYYYMMDD-HHMMSS-<6hex>`, minted once per run. It names the run's
  containers, network, temp dirs and event log — follow it to trace a run
  anywhere.

Job
: One execution request. It comes from a [pipeline config](#pipeline-config),
  from the Submit page, or from the API.

Kind (run kind)
: The type of job: `etl`, `analysis`, `dq_checks`, `health`, `copy`,
  `move`, or `export`. → [Run kinds](../10-reference/run-kinds.md)

Mapper
: A spec listing ops (`translate_by_dict`, `hash_id`, …) that translate
  column values during the map stage. → [Mapper specs](../06-authoring/mappers.md)

MCP
: Model Context Protocol — the standard Agent Ovid uses to expose tools to
  AI clients. → [Agent Ovid (MCP)](../07-assistant/mcp.md)

## P–Z

Pipeline (package)
: A Python package under `ovid2_pipelines.<name>` holding one source's
  formatters, mappers, dictionaries and fixer chains.
  → [Pipelines](../02-concepts/pipelines.md)

Pipeline config
: A saved, schedulable job template: kind, pipeline, input, output,
  options and trigger. Shown on the web *Pipelines* page.

Report cycle (package)
: A named set of EDC checks for one quarterly release (e.g. `20r1`).

Stager
: The bridge that moves data between an engine and a driver (e.g. Snowpark
  ↔ S3).

Transform
: A CDM-agnostic DataFrame op (`trim`, `dedup`, `unpivot`, …) from the
  public library. → [Transforms](../06-authoring/transforms.md)

Trigger
: How a pipeline config starts a job: `manual`, `cron`, `interval` or
  `watch`. → [Scheduling](../04-running-jobs/scheduling.md)

Valueset
: The bounded list of allowed values for a CDM column (e.g. PCORnet
  `DX_TYPE`).

Worker
: `ovid2 web-worker`, the daemon that takes queued jobs and runs them.
