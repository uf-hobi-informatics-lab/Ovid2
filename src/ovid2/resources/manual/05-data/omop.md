---
title: OMOP CDM 5.3
summary: The OHDSI model as Ovid2 registers it — tables, reference order, and concept-driven semantics.
audience: [researcher, developer]
---

`omop_5_3` registers the 18 tables of the OHDSI OMOP Common Data Model
v5.3 that Ovid2 produces:

| Role | Tables |
| --- | --- |
| Health system | `LOCATION`, `CARE_SITE`, `PROVIDER` |
| Person | `PERSON`, `OBSERVATION_PERIOD`, `DEATH` |
| Visits | `VISIT_OCCURRENCE`, `VISIT_DETAIL` |
| Clinical events | `CONDITION_OCCURRENCE`, `DRUG_EXPOSURE`, `PROCEDURE_OCCURRENCE`, `DEVICE_EXPOSURE`, `MEASUREMENT`, `OBSERVATION`, `NOTE`, `SPECIMEN` |
| Relationships & metadata | `FACT_RELATIONSHIP`, `CDM_SOURCE` |

Reference tables, built first: `LOCATION`, `CARE_SITE`, `PROVIDER`,
`PERSON`, `VISIT_OCCURRENCE`.

## How OMOP differs from PCORnet

| Aspect | PCORnet | OMOP |
| --- | --- | --- |
| Coded values | short code + code type (`DX`, `DX_TYPE`) | integer `concept_id` from the OHDSI vocabulary |
| Table placement | by source system (diagnosis vs condition) | by the concept's **domain** (Condition, Drug, Measurement, …) |
| Raw codes | `RAW_*` columns | `*_source_value` + `*_source_concept_id` |
| Missing | `NI` / `UN` / `OT` | `concept_id = 0` (by convention) |

## Concept data

OMOP output depends on the OMOP vocabulary bundle (`CONCEPT`,
`CONCEPT_RELATIONSHIP`, …), installed under
`$OVID2_MATERIAL_DIR/omop_cdm/v5.3/`. Ovid2 exposes it as shared views
that formatters join to. The vocabulary version should be recorded in
`CDM_SOURCE.VOCABULARY_VERSION`.

## Getting OMOP output

There are two routes:

1. **From PCORnet**: the `pcornet_to_omop` pipeline converts a finished
   PCORnet database. See [PCORnet → OMOP](pcornet-to-omop.md).
2. **From an OMOP-native source**: a site pipeline whose formatters use
   the `omop_concept_join` / `semantic_join` adapters to shape OMOP-like
   source tables. In practice, many sites go the other way: from an OMOP
   source extract to PCORnet.
