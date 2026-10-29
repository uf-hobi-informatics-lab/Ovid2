# Changelog

All notable changes to the public Ovid2 distribution are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
the project is pre-1.0 and does not yet follow semantic versioning.

The `main` branch is regenerated from the upstream repository on every
upstream push; each sync commit references the upstream commit it was built
from.

## [Unreleased]

### Fixed — CI green on every gate

- All CI jobs go through `scripts/ci_checks.sh` (`lint`, `typecheck`,
  `contract`, `unit`); run `scripts/ci_checks.sh --docker` to reproduce CI
  locally. ruff and `mypy --strict` are clean; every job installs
  `.[dev,spark,web,mcp,azure]`.
- `ovid2` CLI no longer crashes on Python 3.11+ (duplicate deprecated
  subcommand aliases).
- Live Spark engine metrics (tasks, stages, executors) are reported again.
- Snowpark `regexp_extract` works (it raised `TypeError` on every call).
- A deep-linked run console renders for runs known only from their event log.

### Added — first public snapshot

- **Engine**: lazy five-stage ETL (`format → deduplicate → map → fix →
  write`), per-invocation isolation, typed event bus with TUI / JSONL /
  e-mail / web renderers, `ovid2 etl --local` Docker-less mode.
- **Storage drivers** (9): `disc`, `s3`, `azure_blob`, `snowflake`, `access`
  (Microsoft Access via UCanAccess, with a custom Spark JDBC dialect) natively;
  `gcs`, `databricks`, `postgres`, `mssql` via the legacy adapter.
- **Compute engines** (3): `pyspark`, `databricks_spark`, `snowpark`, with an
  `EngineStager` keeping engine and storage orthogonal.
- **CDMs**: PCORnet 7.0 and OMOP 5.3 as YAML trees; semantic layer for OMOP
  CONCEPT, LOINC, SNOMED, ICD-9/10-CM, RxNorm, NDC.
- **Declarative algorithms**: formatter adapters (`source_passthrough`,
  `semantic_join`, `omop_concept_join`, `union_of_specs`, `custom`), 8 mapper
  ops, 23 common fixers, and a 32-op CDM-agnostic transforms library.
- **Pipelines**: `example_pcornet` (site-neutral PCORnet reference/template)
  and `pcornet_to_omop` (18-table PCORnet → OMOP 5.3 converter).
- **Data quality**: 43 declarative checks (PCORnet EDC `DC 1.01`–`DC 4.03` +
  7 Ovid2-native health panels) in report-cycle packages (`dcq_v7_03`,
  `20r1`), runnable against any PCORnet database.
- **Run kinds**: `etl`, `analysis`, `dq_checks`, `health`, `copy`, `move`
  from a single run-kind registry.
- **Web control panel + JSON API** (88 paths), live run canvas, Data Space
  browser, optional OIDC SSO + 4-role RBAC (off by default), approval gate
  for destructive operations.
- **MCP server** (88 tools) over the HTTP API for AI agents.
- **LLM assistant** with two configurable slots — System 1 (decision model,
  e.g. JEV) and General (any OpenAI-compatible endpoint) — plus a pinned
  44-tool allow-list (read tools, human-approved proposals, draft jobs, and
  page-appearance UI actions). No endpoint or model is pre-configured.
- **Packaging & ops**: PEP 621 wheel, Docker image, systemd units, XDG paths,
  `ovid2 init` / `doctor` / `image` / `service`.
