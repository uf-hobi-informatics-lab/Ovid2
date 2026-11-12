---
title: Fixer chains
summary: Choose, order and parameterise the data corrections applied to each table.
audience: [developer, researcher]
---

A pipeline declares, for each table, an ordered list of **common
fixers** from the shared catalogue. Two forms are accepted; when both
exist, `fixer_configs.yaml` wins.

**YAML** (`fixer_configs.yaml`) — editable in the pipeline builder
(*Fixer chains*, or the *Fix* stage of a table), schema
`resources/schemas/fixer_configs.v1.json`:

```yaml
v: 1
fixer_chains:
  demographic:
    - {fix: race_eth, race_column: RACE, hispanic_column: HISPANIC}
  encounter:
    - {fix: patid_orphans, reference_table: demographic}
multi_table_fixes: []
```

Convert an existing package with `ovid2 tools migrate-fixers --pipeline <name>`
(add `--remove-python` to rename the `.py` to `.py.bak`). Saving from the
builder does the same conversion.

**Python** (`fixer_configs.py`):

```python
FIXER_CHAINS: dict[str, list[dict[str, str]]] = {
    "demographic": [
        {"fix": "race_eth", "src1": "RACE", "src2": "HISPANIC"},
    ],
    "encounter": [
        {"fix": "patid_orphans", "src1": "demographic"},
        {"fix": "providerid_orphans", "src1": "provider", "src2": "PROVIDERID"},
        {"fix": "delete_when_null_or_fon", "src1": "ADMIT_DATE"},
    ],
    "diagnosis": [
        {"fix": "patid_orphans", "src1": "demographic"},
        {"fix": "encounterid_orphans", "src1": "encounter"},
        {"fix": "replication", "src1": "encounter"},
        {"fix": "delete_when_null", "src1": "DX"},
    ],
}

# Fixes that span several tables; run after every per-table chain.
MULTI_TABLE_FIXES: list[dict[str, Any]] = []
```

Each entry has a `fix` name from the registry. The `src1` / `src2` / …
parameters mean different things for each fixer: usually a reference
table, then a column.

## Choosing fixers

| Goal | Fixers |
| --- | --- |
| referential integrity | `patid_orphans`, `encounterid_orphans`, `providerid_orphans` |
| inherit encounter context | `replication`, `copy_admit_date_or_delete` |
| clinical plausibility | `delete_records_before_birth`, `delete_records_after_death`, `illogical_admit_date`, `illogical_discharge_date`, `future_date_to_null` |
| required values | `delete_when_null`, `delete_when_null_or_fon` |
| value hygiene | `invalid_to_null`, `copy_when_null`, `norm_range_normalization` |
| bulk removal | `list_remover`, `patterns_remover` |
| PCORnet-specific | `race_eth`, `loinc_mover`, `panel_loinc_remover`, `null_date_to_harvest_refresh`, `add_missing_hash_token_patids` |

Each fixer, and its scope, in the generated [Fixer catalogue](../10-reference/fixers.md).

## Scopes

Each fixer is declared either **`generic`** (works on any CDM) or scoped
to one CDM (e.g. `pcornet_7_0`). `ovid2 pipeline validate` rejects
unknown fixer names, and fixers whose scope doesn't match the pipeline's
CDM.

## Behaviour at run time

- Fixers run in the listed order, after mapping.
- A fixer that **fails** is logged as `fix.failed`. The table keeps its
  pre-fix data, so a fixer bug can't silently erase rows.
- Cross-table fixers read the already-finished **reference tables**,
  which is why reference tables run first.

> [!NOTE]
> Researchers: fixers are why a delivered table can have fewer rows than
> the raw extract. Each removal is deliberate and documented. See
> [Data lineage](../05-data/data-lineage.md#what-the-fixers-changed).
