---
title: Invariants & contract tests
summary: The design rules that keep Ovid2 coherent, and the contract test that fails the build when each one is broken.
audience: [developer]
---

Ovid2's architecture is not a convention but a **test suite**. Every rule
below has a contract test under `tests/contract/` that runs on every
commit.

## Laziness

| Rule | Test |
| --- | --- |
| No `.collect()`, `.toPandas()`, `.take()`, count-for-branching or intermediate writes in `core/`, `transformations/`, `formatters/`, `mappers/` or `pipelines/` | `test_no_collect_in_src.py` |
| No RDD round-trips | `test_no_rdd_roundtrip.py` |
| Eager code only in generated legacy ports and `src/ovid2/legacy/` | `test_legacy_ports_contained.py` |

## Engine boundaries

| Rule | Test |
| --- | --- |
| Only engine modules (and driver `spark.py` files) import `pyspark` / `snowflake.snowpark` | `test_no_pyspark_outside_engine.py` |
| Only `core/engines/duckdb*.py` import `duckdb` | `test_no_duckdb_outside_engine.py` |
| Shared code imports column functions from `ovid2.core.engines.dsl` | `test_no_runtime_pyspark_in_shared.py` |
| Every engine is registered in both catalogues | `test_engine_registry_complete.py` |
| Every engine has a stager | `test_engine_stager_covers_engines.py` |

## CDM independence

| Rule | Test |
| --- | --- |
| Generic code never names PCORnet | `test_no_pcornet_in_generic.py` |
| Every CDM YAML loads and registers | `test_cdm_yaml_loads.py`, `test_cdm_registry_complete.py` |
| Every fixer declares its scope | `test_fixer_configs_valid.py` |
| Formatter, mapper, transform and health specs validate | `test_formatter_specs_valid.py`, `test_mapper_specs_valid.py`, `test_transform_specs_valid.py`, `test_health_specs_valid.py` |

## Drivers and storage

| Rule | Test |
| --- | --- |
| Every driver implements the ABC and declares its data plane | `test_driver_protocol_complete.py`, `test_driver_data_plane_declared.py` |
| Driver `metadata.py` files import no heavy SDK | `test_driver_metadata_import_safe.py` |
| The five connection catalogues agree | `test_connection_catalogues_synchronised.py` |
| An address is the exact location (nothing appended) | `test_address_is_exact_location.py` |
| Control-tier code never reads rows | `test_control_tier_no_row_access.py` |

## Trust tiers

| Rule | Test |
| --- | --- |
| The MCP tier speaks only HTTP | `test_agent_tier_capability_only.py` |
| The web tier imports no pipeline code, transitively | `test_web_no_pipeline_imports.py`, `test_tier_import_closure.py` |
| The web tier never reads a plaintext secret in a handler | `test_web_no_plaintext_secret_reads.py` |
| Every subprocess gets a scoped env and secret scope | `test_executor_subprocess_env_scoped.py` |
| Destructive operations and agent row reads need approval | `test_destructive_ops_require_approval.py` |
| The assistant's tool catalogue is safe and matches real routes | `test_agent_tools_catalogue.py` |
| Web and MCP never import the LLM runtime | `test_llm_tier_isolation.py` |

## Display, style and surfaces

| Rule | Test |
| --- | --- |
| Pipeline code never imports `display/` | `test_display_isolation.py` |
| Events match the JSON Schema | `test_event_schema.py` |
| No bare `except`, no wildcard imports, no hard-coded paths | `test_no_bare_except.py`, `test_no_wildcard_imports.py`, `test_style_and_paths.py` |
| No third-party CDN in templates | `test_no_third_party_cdn.py` |
| The public API changes only intentionally | `test_web_openapi_stable.py` |
| The manual's links resolve and its reference is current | `test_manual_*.py` |

When one of these fails, **fix the code, not the test**. If an invariant
really has to change, that is a design decision: write an ADR and change
the test in the same commit.
