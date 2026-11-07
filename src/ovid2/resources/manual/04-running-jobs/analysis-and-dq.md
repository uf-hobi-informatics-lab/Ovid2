---
title: Analysis & DQ checks
summary: Profile a source with monthly counts and distinct values, or run a report-cycle DQ check.
audience: [user, researcher, developer]
---

## Analysis

`analysis` runs a pipeline's **format** stage and then summary
transformers instead of map/fix/write-to-CDM. Use it to understand a new
source before you write its mappers:

- which values occur;
- how often;
- across which months.

| Transformer | Produces |
| --- | --- |
| `format` | the formatted table (required first) |
| `monthly_counts` | row counts per table per month of the primary date |
| `distinct_values` | distinct values and frequencies per column, after formatting |
| `raw_distinct_values` | distinct values of the raw source columns (enriched with OMOP concept names where available) |

```bash
ovid2 analysis --pipeline mysite \
  --input_address inbox__2026_03 --output_address scratch__analysis \
  --batch_id inbox/2026_03 --tables all \
  --transformations format monthly_counts distinct_values
```

Analysis needs an **existing** pipeline package; it doesn't scaffold one.
In the web form, pick the package from the *Pipeline package* list. Many
analysis configs can share one package.

> [!TIP]
> Run `raw_distinct_values` first on a brand-new source. The value list is
> usually the fastest way to write its `dictionaries.py`.

## DQ checks

`dq_checks` is the report-cycle form of the [health checks](health-checks.md):
choose one or more cycles (`--packages 20r1`). It reads CDM tables
directly, with no formatter chain and no package required.

```bash
ovid2 dq_checks --input_connection warehouse --input_location CHAR_DEMO/PUBLIC \
  --output_connection warehouse --output_location CHAR_DEMO_QA/PUBLIC --packages 20r1
```
