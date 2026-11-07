---
title: Repository tour
summary: The src-layout package, subsystem by subsystem, plus pipelines, tests and tooling.
audience: [developer]
---

```text
ovid2/
├── pyproject.toml            PEP 621 metadata; ruff, mypy, pytest config; extras
├── src/ovid2/                the package (src layout)
├── pipelines/ovid2_pipelines/  PEP 420 namespace of pipeline packages
├── tests/                    unit/, contract/, integration/, helpers/, fixtures/
├── docker/                   Dockerfile, entrypoint, connector jars, Access dialect
├── configs/                  sample address JSONs
├── scripts/                  CI gates and dev tools
└── docs/                     design documents and diagrams
```

## `src/ovid2/`

| Package | Responsibility | Tier |
| --- | --- | --- |
| `cli/` | `ovid2` argparse dispatch; one module per subcommand with `register()` | executor |
| `core/` | pipeline runner, isolation, docker/compose, loaders, events | executor |
| `core/engines/` | engines: the **only** place that imports `pyspark`, `snowflake.snowpark` or `duckdb`; `dsl.py` is the portable column DSL | executor |
| `formatters/` | spec engine; `adapters/` registry | executor |
| `mappers/` | mapper spec engine; `ops/` | executor |
| `transformations/` | stage orchestrators; `deduplicators/`, `fixers/common/`, `analysis/`, `quality/` (health checks) | executor |
| `transforms/` | the public transforms library | executor |
| `cdm/` | `CdmHandle`, registry, YAML loader | shared |
| `semantic/` | vocabulary handles and bindings | shared |
| `connections/` | connection types and registry (top-level so the web tier can import it) | shared |
| `drivers/` | one folder per storage type: `metadata.py`, `driver.py`, optional `spark.py` / `duckdb.py`, `probe.py` | shared / executor |
| `dataspace/` | the `/<account>/<path…>` namespace | shared |
| `config/` | settings, paths, secrets, scoped executor env, web-safe engine, LLM and transform catalogues | shared |
| `display/` | event bus, JSONL reader, renderers (`tui`, `json_file`, `email_digest`, `plain_log`) | display |
| `web/` | FastAPI app, routes, services, auth, worker, templates, static | API |
| `mcp/` | MCP server and one module per tool | agent |
| `llm/` | executor-only LLM runtime (`python -m ovid2.llm`) | executor |
| `agent_tools/` | tier-neutral loader for the assistant's tool catalogue | shared |
| `manual/` | this manual's loader, renderer, static builder and reference generator | shared |
| `installer/` | `init`, `doctor`, `image`, `service` | host |
| `tools/` | `copy`, `move`, `import-cdm`, `import-vocabulary`, `ods`, table ops, scaffolder | executor |
| `legacy/` | the framework behind generated legacy-parity ports | executor |
| `scheduler/` | the legacy scheduler (rollback only) | executor |
| `run_kinds.py` | the run-kind registry | shared |
| `resources/` | everything shipped as data (see [Configuration](../08-operations/configuration.md#shipped-resources-read-only)) | — |

## Tests

| Directory | Contents | Runs |
| --- | --- | --- |
| `tests/contract/` | repository invariants (80+) | every commit |
| `tests/unit/` | one class or function, mirroring `src/` | every commit |
| `tests/integration/` | multi-component flows with a Spark session | CI |
| `tests/helpers/` | inline DataFrame builders and fixtures | — |
