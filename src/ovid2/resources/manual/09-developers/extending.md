---
title: Extending Ovid2
summary: Step-by-step recipes for every extension point — drivers, engines, CDMs, vocabularies, fixers, mapper ops, transforms, adapters, MCP and assistant tools, LLM providers, CLI subcommands, run kinds, health checks and report sources.
audience: [developer]
---

Each recipe lists the files to touch. The contract test named at the end
of each recipe tells you if you missed one.

## Add a driver

1. Create `src/ovid2/drivers/<type>/` with:
   - `metadata.py`: form and location fields; **no heavy imports**;
   - `driver.py`: the `Driver` subclass, covering control, row access and
     data plane;
   - optional `probe.py` (connection test), `spark.py` (the only file
     allowed to `import pyspark`) and `duckdb.py` (a
     `DuckDBStagingStrategy`).
2. Register it in `drivers/registry.py::DRIVERS`,
   `connections/registry.py::CONNECTION_TYPE_MAP` and `_FIELD_SPECS`,
   plus the remaining catalogues.
3. Declare `spark_capable`, and map the DuckDB strategy in
   `DuckDBEngineStager._STRATEGY_MODULES`.

→ `test_driver_protocol_complete.py`, `test_connection_catalogues_synchronised.py`,
`test_driver_metadata_import_safe.py`

## Add an engine

1. Create `src/ovid2/core/engines/<type>.py` implementing
   `EngineSession` / `EngineFunctions`.
2. Register it in `ENGINE_TYPE_MAP` **and** in the web-safe
   `config/engines.py::_KNOWN_TYPES` plus `_FIELD_SPECS`.
3. Provide stager coverage for the drivers it supports.

→ `test_engine_registry_complete.py`, `test_engine_stager_covers_engines.py`

## Add a CDM

1. Generate the YAML tree from the specification workbook:

   ```bash
   ovid2 tools import-cdm --source <spec.xlsx> --cdm <name> \
     --out-dir src/ovid2/resources/cdms/<name>/
   ```

   Re-running it keeps every hand-written top-level key, such as
   `reference_tables`, `flavour_of_null` and composite-key overrides.
2. Add `src/ovid2/cdm/<name>.py`. It is five lines: `load_cdm(name)` and
   then `register(handle)`.
3. Bounded valuesets register automatically as `<cdm>.<table>.<column>`.

A CDM for a new domain can also ship outside the core tree, in a
[domain pack](domain-packs.md).

→ `test_cdm_yaml_loads.py`, `test_cdm_registry_complete.py`

## Add a vocabulary

1. Extract it: `ovid2 tools import-vocabulary --source <omop_dir> --vocab <slug> --slice-dir … --out-dir src/ovid2/resources/semantic/vocabularies/ --write`.
2. Or write a YAML file matching `schemas/semantic_handle.v1.json` by
   hand.
3. Bind columns in `resources/semantic/bindings/<cdm>.yaml`.

→ `test_semantic_yaml_tree.py`, `test_semantic_registry_complete.py`

## Add a fixer

1. Create `src/ovid2/transformations/fixers/common/<name>.py`, a
   `BaseFixer` subclass. Keep it lazy and import columns from the DSL.
2. Register it in `fixers/common/registry.py::COMMON_FIXES` **and**
   declare its scope in `FIXER_SCOPES` (`generic` or a CDM name).
3. Declare its column role in `config/fixers_catalogue.py::FIXER_COLUMN_ROLES`
   (which columns it reads, writes, or whether it drops rows; grammar in
   `config/column_roles.py`). The [navigator](../03-web-panel/navigator.md)
   uses it to place the fixer on the right fields.
4. Add a unit test with inline DataFrame builders.

→ `test_no_pcornet_in_generic.py`, `test_fixer_configs_valid.py`, `test_column_roles_complete.py`

## Add a mapper op

1. Create `src/ovid2/mappers/ops/<name>.py`, a `BaseMapOp` subclass.
   Document its parameters in a `Params:` block in the module docstring;
   the manual's reference page is generated from it.
2. Register it in `mappers/registry.py::MAP_OPS` and extend the mapper
   JSON Schema.
3. Declare its column role in
   `config/mapper_ops_catalogue.py::MAPPER_OP_COLUMN_ROLES`.

→ `test_mapper_specs_valid.py`, `test_column_roles_complete.py`

## Add a transform

1. Create a `BaseTransform` subclass in `src/ovid2/transforms/ops/` with
   `name`, `summary`, `category`, `params_schema` and, if needed,
   `requires_family`.
2. Register it in `transforms/registry.py::TRANSFORMS` **and** mirror its
   metadata in `config/transforms_catalogue.py`.
3. Declare its column role in
   `config/transforms_catalogue.py::TRANSFORM_COLUMN_ROLES`.

→ `test_transforms_catalogue_synchronised.py`, `test_transforms_registry_complete.py`, `test_column_roles_complete.py`

## Add a formatter adapter

Adapters are framework-level. Never add one per pipeline.

1. Subclass `BaseAdapter` in `src/ovid2/formatters/adapters/`.
2. Register it in `ADAPTERS`, and extend the `adapter` enum in
   `formatter_spec.v1.json`.

→ `test_formatter_specs_valid.py`

## Add an MCP tool

1. Create `src/ovid2/mcp/tools/<tool_name>.py` exposing `TOOL_NAME`,
   `TOOL_DESCRIPTION`, `ARGS_MODEL` (pydantic) and `async run(client,
   args)`. It may make one `/api/v1` call and must import nothing from the
   executor tier.

→ `test_mcp_tools_typed.py`, `test_agent_tier_capability_only.py`

## Add an assistant tool

Add an entry to `resources/agent_tools/catalogue.yaml`. Write tools must
start their description with `PROPOSE`. Never expose secrets, settings,
install, users, approvals, LLMs, storage, the filesystem, or any DELETE.

→ `test_agent_tools_catalogue.py`

> [!NOTE]
> Assistant [sessions](../07-assistant/sessions.md) (saved chats, history,
> search) are **not** agent tools. Never add `/api/v1/llm/…` routes to the
> catalogue: the contract test forbids that prefix, and the assistant's
> per-turn token is refused by the session routes anyway.

## Add an LLM provider

1. Create `src/ovid2/llm/providers/<name>.py` implementing `decide()`
   and/or `chat_stream()`.
2. Register it in `PROVIDER_TYPE_MAP`, and in
   `config/llms.py::_KNOWN_PROVIDERS`, `_PROVIDER_ROLES` and `_FIELD_SPECS`.

→ `test_llm_tier_isolation.py`

## Add a CLI subcommand

1. Create a module with `register(subparsers)`, and set
   `_entry=<function>` on its parser.
2. Add it to `_SUBCOMMANDS` in `cli/__main__.py`.
3. New `*_address` / `*_connection` arguments must also be added to
   `core/docker.py::SCOPED_ADDRESS_ARGS` / `SCOPED_CONNECTION_ARGS`.
4. Regenerate the manual's reference: `ovid2 manual gen-reference`.

→ `test_subcommand_registration.py`, `test_manual.py`

## Add a run kind

A run kind (`etl`, `health`, `copy`, …) is one declarative `KindSpec`
entry. The Pipelines page grouping, the New-pipeline dropdown,
create/submit validation, the worker argv and the container `/data` mount
rule all follow from it.

1. Add a `KindSpec` to `KIND_REGISTRY` in `src/ovid2/run_kinds.py`. This
   module is importable by both the executor and the web tier, so keep it
   free of heavy imports. Fields:

   | Field | Meaning |
   | --- | --- |
   | `name`, `label`, `description` | registry key, dropdown label, one-line text |
   | `package_required` | the executor imports an `ovid2_pipelines.<name>` package |
   | `cli_subcommand` | the `ovid2` subcommand the worker runs (e.g. `tools copy`) |
   | `argv_builder` | `etl`, `health`, `copy`, `move` or `export` |
   | `uses_transformations`, `uses_cdm` | when `False`, stale values are scrubbed |
   | `tables_mode` | `all`, `explicit` or `none` |
   | `allows_extra_mounts`, `allows_strict_types`, `health_selection` | per-kind fields |
   | `read_only_data_mount` | mount `/data` read-only in the container |
   | `create_flow` | `scaffold_package`, `pick_package`, `cycle_packages` or `plain` |
   | `job_only` | a one-off job (like `export`), never offered as a pipeline |

2. If the kind needs its own command, add it first. See
   [Add a CLI subcommand](#add-a-cli-subcommand). A new argv shape means a
   new `ArgvBuilder` value **and** a `Worker._build_argv_<builder>` method
   in `src/ovid2/web/worker.py`.
3. **UI rule U1.** Never write `if kind == "…"` in a template or JS. A new
   behaviour difference becomes a new `KindSpec` field, exposed to the
   browser by `ovid2.web.kinds.kind_flags`. Forms then mark rows with
   `data-show-kind-flag="<flag>"` and `static/field_visibility.js` hides
   what doesn't apply.
4. Update the frozen expectations in the contract tests (the `/data`
   read-only / read-write sets and the job-only set) and the run-kind ADR.
5. Run `ovid2 manual gen-reference` to refresh
   [Run kinds](../10-reference/run-kinds.md).

→ `test_run_kind_registry_complete.py`, `test_route_kind_valid.py`, `test_ui_rules.py`

## Add a health check or operator

A health check is a YAML spec that names an **operator**. Most new checks
reuse an existing operator with different `params`.

1. Create `src/ovid2/resources/health/checks/<id>.yaml`. It must validate
   against `resources/schemas/health_check.v1.json`:

   ```yaml
   id: "health.demo_null_rate"     # unique; [A-Za-z0-9_.]
   name: DEMO_NULL_RATE
   part: completeness              # part1–part4, mapping, freshness, integrity, completeness, semantic
   description: Too many null values in DEMOGRAPHIC
   operator: null_rate             # a registered operator
   source_tables: [DEMOGRAPHIC]    # absent tables auto-skip the check
   na_when_empty: [DEMOGRAPHIC]    # optional, see below
   params:                         # operator-specific
     treat_flavour_of_null_as_missing: true
   metrics: [TOTAL_ROWS, NULL_ROWS, NULL_PCT]
   exception_metrics: [NULL_PCT]
   thresholds: {null_pct_gt: 50.0}
   doc_summary: >-
     What the check measures.
   doc_interpretation: >-
     How to read its metric rows.
   severity: warning               # blocker | warning | info
   applies_to_cdms: [pcornet_7_0]  # optional; other CDMs report n/a
   remediation:                    # optional; feeds the repair context
     hint: Normalise blank values, then fix the extract.
     transforms: [standardize_null]
   ```

2. `na_when_empty` lists tables whose **collective** emptiness makes the
   check `not_applicable` instead of `passed` / `exception`. The check is
   n/a only when *every* listed table is absent or has zero rows. If any
   one is populated, the check keeps its computed status.
3. To ship it in a report cycle, add its id to a package under
   `src/ovid2/resources/health/packages/<cycle>.yaml`.
4. A new operator goes in `src/ovid2/transformations/quality/ops/__init__.py`:

   ```python
   @operator("my_check")
   def _my_check(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]: ...
   ```

   Keep it lazy and engine-portable. Read tables with `ctx.table(name)`,
   test presence with `ctx.has(name)`, build column expressions from
   `ctx.functions`, and use `ctx.rows(df)` (the runner's counter) for the
   few counts you need. Never import an engine library.
5. Add a unit test under `tests/unit/quality/` (for example
   `test_health_ops.py`).

To ship checks for a new domain without editing core, put them in a
[domain pack](domain-packs.md) instead.

→ `test_health_specs_valid.py`, `test_health_packages_valid.py`,
`test_check_remediation_resolves.py`

## Add a report source kind or chart kind

### A source kind

A *report source* is a durable bundle of small, aggregated datasets
(`resources/schemas/report_source.v1.json`) that
[Report Studio](../05-data/report-studio.md) layouts draw from.

1. Write `src/ovid2/reports/sources/<kind>.py` exposing
   `build(<artifact>, **meta) -> dict`. It is a pure function. Build
   columns with `column(...)` and datasets with `dataset(...)`, then return
   `make_source(...)`, which validates the document. Tag a column
   `sensitivity="row_values"` when it may carry raw source values, so it is
   masked for agents and users without `VIEW_PIPELINE_DATA`. Mark record or
   patient counts `is_count=True` so small-cell suppression applies.
2. Register it in `_registry()` in `src/ovid2/reports/sources/__init__.py`
   (the `ADAPTERS` map), with a label and description.
3. Ship at least one template under
   `src/ovid2/resources/reports/templates/<kind>/<name>.yaml`.
4. Add a sample for the kind to `sample_sources()` in
   `src/ovid2/reports/samples.py`. The contract test renders every template
   against it, under every report theme.
5. To ingest it automatically when a job finalises, add a step to
   `ingest_for_instance` in `src/ovid2/web/services/report_sources_svc.py`.

`ovid2.reports` must stay tier-neutral: no web, MCP, LLM, core, driver or
document-library imports outside `ovid2.reports.render`.

### A chart kind

Charts are a curated kind plus params. Users and the assistant never write
Vega-Lite.

1. Add an entry under `charts:` in `src/ovid2/resources/reports/charts.yaml`:
   `name`, `label`, `category` (one of `categories:`), `renderer: vega`,
   `summary`, and `params`, a JSON Schema. Mark column parameters with
   `x-field: [<column types>]` or `x-fields: [...]` so field pickers and
   validation work.
2. Write the builder in `src/ovid2/reports/charts.py`, with the signature
   `(params, rows, cols, theme) -> spec`, and register it in `BUILDERS`
   under the same name.

→ `test_report_catalogues.py`
