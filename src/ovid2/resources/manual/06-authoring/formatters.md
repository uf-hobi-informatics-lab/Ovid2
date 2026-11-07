---
title: Formatter specs
summary: The YAML that builds one CDM table — sources, joins, projections, adapters — with complete field reference.
audience: [developer]
---

A formatter spec describes how one CDM table is built from raw sources.
It lives at `formatters/<table>.yaml`, or `formatters/<table>.py` with a
module-level `SPEC = spec(...)`. When both exist, the Python module wins.

## Minimal spec

```yaml
v: 1
formatter: demographic
adapter: source_passthrough

sources:
  demographic:
    glob: "*Demographic_*.txt"

projections:
  PATID: patid
  BIRTH_DATE: birth_date
  SEX: sex
  RAW_SEX: sex
  ZIP_CODE: "''"          # SQL literal: empty string

output_columns_from: pcornet_7_0.DEMOGRAPHIC
```

## Fields

| Field | Type | Required by | Purpose |
| --- | --- | --- | --- |
| `v` | int | always | schema version (`1`) |
| `formatter` | string | always | output CDM table name (snake_case) |
| `adapter` | enum | always | `source_passthrough`, `semantic_join`, `omop_concept_join`, `union_of_specs`, `custom` |
| `enabled` | bool | optional | `false` skips the table |
| `input_file_name` | string | optional | overrides the primary glob (e.g. for `custom`) |
| `auxiliary_tables` | list | optional | temp views to register before running |
| `sources` | mapping | passthrough / join adapters | named source blocks (below) |
| `joins` | list | join adapters | joins to `concept` or another source |
| `projections` | mapping | passthrough / join adapters | `OUTPUT_COL: sql_expression` |
| `post_expressions` | list | optional | extra `selectExpr` items applied after projections |
| `output_columns_from` | string | optional | `<cdm>.<TABLE>`: validate the output columns against the CDM |
| `sub_specs` | list | `union_of_specs` | sub-specs to build and union |
| `class` | string | `custom` | dotted path to a `BaseFormatter` subclass |
| `requires_family` | string | optional | `spark` for Spark-only custom formatters |

The JSON Schema is at `src/ovid2/resources/schemas/formatter_spec.v1.json`.

### Source block

```yaml
sources:
  condition_occurrence:
    glob: "*condition_occurrence*"      # matched in the input address location
    exclude: ["*_sup_*"]                # optional: basenames to subtract
    optional: false                     # true: a missing file yields an empty table + warning
    filter: "condition_type_concept_id in ('32840', '32865')"   # SQL predicate
    lowercase_columns: true             # normalise headers (default true)
```

### Join block

```yaml
joins:
  - with: concept                                              # a shared view or another source
    on: "concept.concept_id = condition_occurrence.condition_concept_id"
    how: left                                                  # left|inner|right|full|left_semi|left_anti
    prefix: cond                                               # joined columns become cond_<col>
    domain_filter: ["Condition"]                               # only for `with: concept`
```

Every column of the joined side is aliased with `prefix`, so later
projections refer to it as `cond_concept_code`, `cond_vocabulary_id`
and so on. The prefix rules out ambiguous-column bugs.

## Adapters

`source_passthrough`
: Reads one or more sources, unions them by name, and applies the
  projections. Use it for data that already has the right shape. The old
  name `pcornet_passthrough` is a deprecated alias.

`semantic_join`
: Like passthrough, plus joins to any registered vocabulary.
  `omop_concept_join` is its OMOP-`CONCEPT` compatibility form.

`union_of_specs`
: Builds several sub-specs and unions their outputs. For example, `VITAL`
  from height, weight and blood-pressure sources.

`custom`
: Names a `BaseFormatter` subclass. This is the escape hatch for logic
  YAML can't express. Don't put Spark-only APIs in it unless you set
  `requires_family: spark`.

Every adapter, generated from the code: [Formatter adapters](../10-reference/adapters.md).

## Python-dict form

```python
from ovid2.formatters import spec

SPEC = spec(
    formatter="condition",
    adapter="omop_concept_join",
    sources={"condition_occurrence": {"glob": "*condition_occurrence_*.csv"}},
    joins=[
        {
            "with": "concept",
            "on": "concept.concept_id = condition_occurrence.condition_concept_id",
            "how": "left",
            "prefix": "cond",
            "domain_filter": ["Condition"],
        }
    ],
    projections={
        "CONDITIONID": "condition_occurrence.condition_occurrence_id",
        "PATID": "condition_occurrence.person_id",
        "CONDITION": "cond_concept_code",
    },
    output_columns_from="pcornet_7_0.CONDITION",
)
```

It means exactly the same as the equivalent YAML.

## Rules

> [!IMPORTANT]
> - Projections are **Spark SQL expressions**. On DuckDB they are
>   transpiled automatically, with Spark-compatible date parsing.
> - Don't call `.collect()`, `.count()` or `.toPandas()`, or write
>   intermediate files, in custom formatters. A run is one lazy plan.
> - Never add a per-pipeline adapter. Adapters are framework-level.
> - Don't hand-edit `formatters_custom/*` in `legacy_ovid` packages; they
>   are generated.

You can edit and validate specs in the web panel: open **Pipelines →
Packages → <package> → formatter**.
