---
title: PCORnet CDM 7.0
summary: The default output model — its tables, keys, valuesets and conventions as Ovid2 implements them.
audience: [researcher, developer]
---

`pcornet_7_0` is Ovid2's default CDM. It mirrors the official PCORnet
Common Data Model v7.0 specification, imported directly from its
parseable workbook, plus a few Ovid-specific tables. The registered model
has 28 tables and 133 bounded valuesets.

## Tables by role

| Role | Tables |
| --- | --- |
| Patient | `DEMOGRAPHIC`, `ENROLLMENT`, `DEATH`, `DEATH_CAUSE`, `LDS_ADDRESS_HISTORY`, `PAT_RELATIONSHIP` |
| Encounters & providers | `ENCOUNTER`, `PROVIDER` |
| Clinical events | `DIAGNOSIS`, `PROCEDURES`, `CONDITION`, `VITAL`, `OBS_CLIN`, `OBS_GEN`, `PRO_CM` |
| Medications | `PRESCRIBING`, `DISPENSING`, `MED_ADMIN`, `IMMUNIZATION`, `EXTERNAL_MEDS` |
| Labs | `LAB_RESULT_CM`, `LAB_HISTORY` |
| Linkage | `HASH_TOKEN` (and Ovid's `ORIGINAL_HASH_TOKEN`) |
| Trials | `PCORNET_TRIAL` |
| Datamart metadata | `HARVEST` |

Full field lists, keys and descriptions: the [CDM reference](../10-reference/cdms.md#pcornet_7_0).

## Keys and de-duplication

Each table's composite key (e.g. `PATID` for `DEMOGRAPHIC`,
`ENCOUNTERID` for `ENCOUNTER`, `PATID + DEATH_SOURCE` for `DEATH`) drives
the deduplicate stage. **A delivered table has no duplicate keys.** Rows
with a null key component are kept, because they can't be duplicates.

## Reference tables and referential integrity

`DEMOGRAPHIC`, `PROVIDER`, `HARVEST`, `ENCOUNTER` and `DEATH` are built
first. Child tables are then checked against them by fixers that the
pipeline chooses:

| Fixer | Effect |
| --- | --- |
| `patid_orphans` | removes rows whose `PATID` is not in `DEMOGRAPHIC` |
| `encounterid_orphans` | removes rows whose `ENCOUNTERID` is not in `ENCOUNTER` |
| `providerid_orphans` | blanks or removes unknown `PROVIDERID`s |
| `replication` | copies encounter attributes (dates, type) onto child rows |

See [Fixer catalogue](../10-reference/fixers.md) for every correction.

## Flavours of null

PCORnet distinguishes *no information* (`NI`), *unknown* (`UN`) and
*other* (`OT`) from a true blank. Ovid2 treats `NI`, `OT`, `UN`, the
empty string and null as the model's **flavour-of-null** set. This
matters for:

- `delete_when_null_or_fon`, which removes rows missing a required value;
- `enforce`-style checks, which map values outside a valueset to `OT`;
- the health panel **null rate**.

## HARVEST

`HARVEST` describes the datamart and the refresh: network and datamart
ids, CDM version, refresh dates per table. Its identity values come from
the pipeline's `pipeline_settings.py`. In the public `example_pcornet`
package they are placeholders.

## Valuesets

Every bounded field (`SEX`, `HISPANIC`, `DX_TYPE`, `ENC_TYPE`, …) has its
allowed values in `resources/cdms/pcornet_7_0/valuesets/`. The health
panel **valueset conformance** reports the share of values outside the
set. The CDM detail page in the panel lists the allowed values for each
field.
