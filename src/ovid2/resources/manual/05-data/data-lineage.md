---
title: Data lineage
summary: How to trace any delivered value back to its source file, the rule that shaped it, and every correction applied.
audience: [researcher, developer]
---

Ovid2's creed, *fidelity over fortune*, means every value in the output
can be explained. This page shows you how.

## The chain for one value

```text
source file / table  ──format──►  CDM column  ──map──►  standard value  ──fix──►  delivered value
  (sources: glob)                  (projections)        (ops + dictionaries)      (fixer chain)
```

For any column of any table you can read, in the pipeline package:

| Question | Where to look |
| --- | --- |
| Which source files fed this table? | `formatters/<table>.yaml` → `sources:` (globs, filters, excludes) |
| Which source column became this field? | `formatters/<table>.yaml` → `projections:` (`OUTPUT: source_expression`) |
| How was the value translated? | `mappers/<table>.yaml` → `ops:`, plus `dictionaries.py` for `translate_by_dict` |
| Which corrections ran? | `fixer_configs.py` → `FIXER_CHAINS["<table>"]` |
| What did the CDM require? | `resources/cdms/<cdm>/tables/<table>.yaml` and `valuesets/` |

The web panel shows all of this. Under **Pipelines → Packages →
<package>**, open a formatter or mapper to read its spec. Open
**Configure → CDMs** for the model.

## Raw values are preserved

PCORnet output keeps the original source value next to the standardised
one in `RAW_*` columns (`RAW_SEX`, `RAW_DX`, `RAW_LAB_NAME`, …). OMOP
keeps it in `*_source_value`. If a mapping looks wrong, compare the two.

## What the fixers changed

Every fixer emits a structured event for every table it touches:

| Event | Meaning |
| --- | --- |
| `fix.applied` | the fixer ran on this table |
| `fix.skipped` | it was configured but didn't apply (e.g. a missing column) |
| `fix.failed` | it raised. The table kept its **pre-fix** data, and the error is recorded. |

Find them on the run page's timeline (**Fixes** column), or in the event
log:

```bash
jq -c 'select(.kind|startswith("fix.")) | {table, fix, kind, error}' events-<instance_id>.jsonl
```

To see what a fixer does, look it up in the
[fixer catalogue](../10-reference/fixers.md).

## Reproducing a delivery

A delivery is reproducible from four things:

1. the **input** location (the batch id is its full path);
2. the **pipeline package** version, from git;
3. the Ovid2 **version** (`ovid2 --version`), and the image it ran in;
4. the **engine** (hash-derived surrogate ids differ between engines).

All four are recorded with the run, under its instance id.
