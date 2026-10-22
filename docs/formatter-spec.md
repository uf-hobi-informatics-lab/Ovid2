# Formatter spec reference

Ovid2 formatters are **declarative**. Pipeline engineers describe how a
raw source becomes a CDM table by picking an *adapter* and writing a
YAML (or Python-dict) spec — no boilerplate, no Spark session
management, no per-table script.

## File layout

```
ovid2_pipelines/<pipeline>/
  __init__.py
  pipeline_settings.py
  formatters/
    __init__.py
    condition.yaml          # YAML spec
    encounter.py            # Python-dict spec (module-level SPEC = spec(...))
  formatters_custom/        # optional — for adapter: custom
    __init__.py
    vital_formatter.py      # subclasses BaseFormatter
```

The loader (:func:`ovid2.formatters.spec.find_and_load`) prefers Python
modules over YAML when both are present.

## Minimum spec

```yaml
v: 1
formatter: demographic
adapter: pcornet_passthrough

sources:
  demographic:
    glob: "*Demographic*"

projections:
  PATID: "patid"
  BIRTH_DATE: "birth_date"
  SEX: "sex"
```

## Fields

| Field | Type | Required by | Purpose |
| --- | --- | --- | --- |
| `v`                    | int         | always | Schema version. Currently `1`. |
| `formatter`            | string      | always | Output CDM table name (snake_case). |
| `adapter`              | enum        | always | One of `pcornet_passthrough`, `omop_concept_join`, `union_of_specs`, `custom`. |
| `enabled`              | bool        | optional | `false` skips the table (replaces legacy `_not_provided` suffix). |
| `input_file_name`      | string      | optional | Overrides the primary glob when needed (e.g. for the custom adapter). |
| `auxiliary_tables`     | [string]    | optional | Temp views to register before running. |
| `sources`              | mapping     | `passthrough`, `omop_concept_join` | Named source blocks (see below). |
| `joins`                | [join]      | `omop_concept_join` | Joins to `concept` or another source. |
| `projections`          | mapping     | `passthrough`, `omop_concept_join` | `OUTPUT_COL: sql_expr`. |
| `post_expressions`     | [string]    | optional | Extra `selectExpr` items applied after `projections`. |
| `output_columns_from`  | string      | optional | `pcornet_7_0.CONDITION`. Validates the output columns match the CDM. |
| `sub_specs`            | [ref]       | `union_of_specs` | Sub-specs to compose and union. |
| `class`                | string      | `custom` | Dotted path to a `BaseFormatter` subclass. |

### Source block

```yaml
sources:
  condition_occurrence:
    glob: "*condition_occurrence*"     # file glob (pipeline-scoped)
    exclude: ["*_sup_*"]               # optional glob subtraction (basename)
    optional: false                    # missing files fail unless true
    filter: "condition_type_concept_id in ('32840', '32865')"  # SQL predicate
    lowercase_columns: true            # normalize headers (default true)
```

### Join block

```yaml
joins:
  - with: concept                                            # temp view or source name
    on: "concept.concept_id = condition_occurrence.condition_concept_id"
    how: left                                                # left|inner|right|full|left_semi|left_anti
    prefix: cond                                             # every joined column becomes cond_<col>
    domain_filter: ["Condition"]                             # only for `with: concept`
```

The adapter aliases every column of the right side with the `prefix`,
so downstream projections reference `cond_concept_code`,
`cond_vocabulary_id`, etc. This structurally prevents the ambiguous
`concept_id` bug that historically dogged OMOP formatters.

## Adapters

- **`pcornet_passthrough`** — for pipelines whose source data is already
  PCORnet-shaped. Reads one or more sources, unions them by name, and
  hands the wide DataFrame to the projections.

- **`omop_concept_join`** — for OMOP source data. Reads the sources
  (lowercases headers, applies excludes, filters), then joins the
  shared `concept` view once per entry in `joins:`. Every join column
  is prefix-aliased and every domain-filter is applied *before* the
  join.

- **`union_of_specs`** — composes several sub-specs and unions their
  outputs. Used by outputs like VITAL that draw from
  `measurement` ∪ `observation` ∪ `observation_sup`.

- **`custom`** — escape hatch. The spec names a dotted path to a
  `BaseFormatter` subclass; use only for outputs that cannot be
  expressed declaratively.

## Python-dict variant

Prefer this when your reviewers all read Python:

```python
# ovid2_pipelines/site/formatters/condition.py
from ovid2.formatters import spec

SPEC = spec(
    formatter="condition",
    adapter="omop_concept_join",
    sources={
        "condition_occurrence": {
            "glob": "*condition_occurrence_*.csv",
            "exclude": ["*_sup_*.csv"],
            "filter": "condition_type_concept_id in ('32840', '32865', '38000245')",
        },
    },
    joins=[
        {
            "with": "concept",
            "on":   "concept.concept_id = condition_occurrence.condition_concept_id",
            "how":  "left",
            "prefix": "cond",
            "domain_filter": ["Condition"],
        },
    ],
    projections={
        "CONDITIONID":  "condition_occurrence.condition_occurrence_id",
        "PATID":        "condition_occurrence.person_id",
        "ONSET_DATE":   "condition_occurrence.condition_start_date",
        "CONDITION":    "cond_concept_code",
        "CONDITION_TYPE": "cond_vocabulary_id",
    },
    output_columns_from="pcornet_7_0.CONDITION",
)
```

Semantically identical to the equivalent YAML.

## Contract tests that guard specs

- `tests/contract/test_formatter_specs_valid.py` — every spec on the
  search path validates against `formatter_spec.v1.json`.
- `tests/contract/test_no_collect_in_src.py` — banned Spark actions in
  the engine and any bundled adapters.

## Adding a new adapter

Adapters live under `src/ovid2/formatters/adapters/`. Add a new class
that subclasses :class:`ovid2.formatters.adapters.base.BaseAdapter`,
register it in `adapters/__init__.py::ADAPTERS`, and extend the JSON
schema `adapter` enum. New adapters are **framework-level** — do not
put per-pipeline adapters in `ovid2_pipelines/*`.
