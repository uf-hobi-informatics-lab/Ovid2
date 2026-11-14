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
| Every transform op, mapper op and fixer declares a column role (used by the navigator's lineage) | `test_column_roles_complete.py` |

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
| UI rules: only relevant fields shown (registry flags, no kind literals); closed sets are selects; explanations under "?" | `test_ui_rules.py` |
| The public API changes only intentionally | `test_web_openapi_stable.py` |
| The manual's links resolve and its reference is current | `test_manual_*.py` |

## UI rules

Every web form follows three hard rules. They are enforced, not advisory:

1. **Only relevant fields are shown.** Mark a row
   `data-show-kind-flag="uses_cdm"` (or `data-show-kind`,
   `data-show-engine-family`, `data-hide-engine-family`,
   `data-show-when="trigger_type=cron"`) inside a
   `data-visibility-root`, emit the `#kind-flags` JSON island
   (`kind_choices()`), and `static/field_visibility.js` hides the rest.
   Never compare against a kind name in JS or a template. If a new kind
   behaves differently, add a flag to `ovid2.web.kinds.kind_flags`.
   Read-only pages use the `kind_spec(kind)` Jinja global.
2. **A value from a known set is a select.** Catalogue fields declare
   `widget: "choices"` with `choices` (shared tuples in
   `drivers/_choices.py`) or `choices_from: "connections:<type>"`, and
   render through `templates/_field.html`. `<datalist>` only with
   `data-allow-new`.
3. **Explanations live under "?".** Use `help.tip()` /
   `help.tip_block()` from `templates/_help.html`, or
   `Ovid2HelpTip.tip()` / `.html()` from JS. Status, error, empty-state
   and confirmation text stays visible.

When one of these fails, **fix the code, not the test**. If an invariant
really has to change, that is a design decision: write an ADR and change
the test in the same commit.
