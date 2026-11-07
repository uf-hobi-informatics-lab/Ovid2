---
title: Extending Ovid2
summary: Step-by-step recipes for every extension point — drivers, engines, CDMs, vocabularies, fixers, mapper ops, transforms, adapters, MCP tools and LLM providers.
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
3. Add a unit test with inline DataFrame builders.

→ `test_no_pcornet_in_generic.py`, `test_fixer_configs_valid.py`

## Add a mapper op

1. Create `src/ovid2/mappers/ops/<name>.py`, a `BaseMapOp` subclass.
   Document its parameters in a `Params:` block in the module docstring;
   the manual's reference page is generated from it.
2. Register it in `mappers/registry.py::MAP_OPS` and extend the mapper
   JSON Schema.

→ `test_mapper_specs_valid.py`

## Add a transform

1. Create a `BaseTransform` subclass in `src/ovid2/transforms/ops/` with
   `name`, `summary`, `category`, `params_schema` and, if needed,
   `requires_family`.
2. Register it in `transforms/registry.py::TRANSFORMS` **and** mirror its
   metadata in `config/transforms_catalogue.py`.

→ `test_transforms_catalogue_synchronised.py`, `test_transforms_registry_complete.py`

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

→ `test_subcommand_registration.py`, `test_manual_reference_fresh.py`
