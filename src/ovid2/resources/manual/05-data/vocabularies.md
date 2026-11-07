---
title: Vocabularies
summary: The clinical code systems Ovid2 knows, where they come from, and how mappers and checks use them.
audience: [researcher, developer]
---

Ovid2's **semantic layer** registers clinical vocabularies so that
formatters, mappers and health checks can refer to them by name.

| Vocabulary | Contents | Source |
| --- | --- | --- |
| `omop_concept` | every OMOP standard and source concept (60+ vocabularies) | OMOP `CONCEPT` |
| `icd10cm` | ICD-10-CM diagnoses | OMOP slice |
| `icd9cm` | ICD-9-CM diagnoses (legacy claims) | OMOP slice |
| `loinc` | LOINC lab and observation codes | OMOP slice |
| `snomed` | SNOMED CT concepts | OMOP slice |
| `rxnorm` | RxNorm clinical drugs (RxCUI) | OMOP slice |
| `ndc` | 11-digit National Drug Codes | OMOP slice |
| `<cdm>.<table>.<column>` | every bounded CDM valueset, e.g. `pcornet_7_0.demographic.sex` | CDM YAML |

Each vocabulary is a YAML file in
`src/ovid2/resources/semantic/vocabularies/`. It names its code,
description and domain columns, and the slice file that holds them. The
slices are extracted from the OMOP bundle with:

```bash
ovid2 tools import-vocabulary --source <omop_dir> --vocab loinc snomed \
  --slice-dir "$OVID2_MATERIAL_DIR/omop_cdm/v5.3/vocab_slices/" \
  --out-dir src/ovid2/resources/semantic/vocabularies/ --write
```

## Column bindings

A **binding** says *"this CDM column holds codes from that vocabulary"*.
For example, `LAB_RESULT_CM.LAB_LOINC` → `loinc`. Bindings for PCORnet
live in `resources/semantic/bindings/pcornet_7_0.yaml`.

## How vocabularies are used

| Consumer | Use |
| --- | --- |
| `semantic_join` / `omop_concept_join` adapters | join source rows to concepts while formatting |
| `translate_by_concept` mapper op | translate a column through a vocabulary |
| health panel `mapping_coverage` | share of codes found in their bound vocabulary |
| health panel `valueset_conformance` | share of values inside the CDM valueset |

Inspect them from the CLI:

```bash
ovid2 semantic list --kind vocabulary
ovid2 semantic show vocabulary loinc
ovid2 semantic bindings --cdm pcornet_7_0
```
