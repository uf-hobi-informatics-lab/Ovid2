---
title: PCORnet → OMOP conversion
summary: How the pcornet_to_omop pipeline maps each PCORnet table to OMOP, resolves concepts, builds ids — and its known limitations.
audience: [researcher, developer]
---

`pcornet_to_omop` is an `etl` pipeline. It reads a finished PCORnet 7.0
database and writes OMOP CDM 5.3 tables. Run it like any other ETL
job, with the PCORnet schema as input and an empty OMOP schema as output.
On Snowflake, `--engine snowpark` converts entirely inside the warehouse.

## Table map

| OMOP table | PCORnet source(s) |
| --- | --- |
| `PERSON` | `DEMOGRAPHIC` |
| `VISIT_OCCURRENCE` | `ENCOUNTER` |
| `CONDITION_OCCURRENCE` | `DIAGNOSIS` + `CONDITION` |
| `DRUG_EXPOSURE` | `MED_ADMIN` + `PRESCRIBING` + `DISPENSING` + `IMMUNIZATION` |
| `MEASUREMENT` | `LAB_RESULT_CM`, `PROCEDURES`, `OBS_CLIN`, `OBS_GEN`, `VITAL`: rows whose code is in the Measurement domain |
| `OBSERVATION` | the same sources: rows in the Observation domain |
| `PROCEDURE_OCCURRENCE` | the same sources: rows in the Procedure domain |
| `DEATH` | `DEATH` + `DEATH_CAUSE` |
| `OBSERVATION_PERIOD` | `ENROLLMENT` (one period per enrollment row) |
| `LOCATION` | distinct `ENCOUNTER.FACILITY_LOCATION` |
| `CARE_SITE` | distinct `ENCOUNTER.FACILITYID` |
| `PROVIDER` | `PROVIDER` |
| `SPECIMEN` | `LAB_RESULT_CM` specimen fields |
| `CDM_SOURCE` | `HARVEST` + pipeline settings |
| `DEVICE_EXPOSURE`, `NOTE`, `VISIT_DETAIL`, `FACT_RELATIONSHIP` | no PCORnet source; written empty so the schema is complete |

`VITAL` is unpivoted into one measurement or observation per vital sign:

- weight, height and BMI, as LOINC codes;
- systolic and diastolic blood pressure, with the LOINC chosen by
  `BP_POSITION`;
- smoking and tobacco status.

Blank and `NI` / `UN` / `OT` vitals are skipped.

## Concept resolution

1. **Source concept.** The PCORnet code and its code type (for example
   `DX` + `DX_TYPE = 10`) are matched to a valid OMOP `CONCEPT` in the
   target domain. The code type maps to a vocabulary: `10` → ICD10(CM/PCS),
   `09` → ICD9(CM/Proc), `LC` → LOINC, `SM` → SNOMED, `RX` → RxNorm,
   `ND` → NDC, `CH` → CPT4/HCPCS, `CX` → CVX.
2. **Standard concept.** The source concept is followed through
   `CONCEPT_RELATIONSHIP` `'Maps to'`. If there are several targets, the
   most recently valid one wins.
3. `*_source_value` keeps the raw PCORnet code, and `*_source_concept_id`
   keeps the concept from step 1.

Short coded fields (gender, race, ethnicity, visit type, discharge status,
route, unit, …) use documented static dictionaries in the pipeline's
`dictionaries.py`.

**Domain routing uses the source concept's domain.** A lab coded with a
LOINC in the Observation domain lands in `OBSERVATION`, not
`MEASUREMENT`.

## Identifiers

- **Natural keys are reused.** For example, `person_id` = `PATID`,
  `visit_occurrence_id` = `ENCOUNTERID`, `condition_occurrence_id` =
  `DIAGNOSISID` / `CONDITIONID`. Vital rows use `<VITALID>_<measure>`.
- **Tables without a natural key get a deterministic hash** of their
  defining columns: `observation_period_id`, `location_id`,
  `care_site_id`, `provider_id`. Hashes are stable from run to run **on the
  same engine**, but differ between engines.

## Dates

PCORnet dates are parsed as `yyyy-MM-dd`. Datetimes combine the date with
the PCORnet time, or `00:00:00` if there is none. Event dates that are
missing or can't be parsed become **`1900-01-01`**. Exclude that sentinel
in analyses.

## Known limitations

> [!WARNING]
> Review these before using converted data for analysis. They describe
> the current implementation; they are tracked for correction.
>
> - **Unresolvable codes are dropped, not set to 0.** A condition, drug,
>   measurement, observation or procedure row whose code has no valid
>   source concept in the target domain is excluded. A code with a source
>   concept but no `'Maps to'` target is kept, with a **null**
>   `*_concept_id` instead of 0.
> - **Some foreign keys don't join to their dimension tables.**
>   `CARE_SITE.care_site_id` and `PROVIDER.provider_id` are hashed, but
>   `VISIT_OCCURRENCE.care_site_id` and the `provider_id` columns of event
>   tables carry the raw PCORnet ids.
> - **Ids may collide across sources.** `condition_occurrence_id` can
>   repeat if a `DIAGNOSISID` equals a `CONDITIONID`. The same applies to
>   the other multi-source tables.
> - **Diagnoses outside the Condition domain are dropped**, not rerouted
>   to `OBSERVATION` or `MEASUREMENT`.
> - **`OBSERVATION_PERIOD` mirrors `ENROLLMENT`**: one row per
>   enrollment row, not merged spans derived from events.
> - **`provider.specialty_concept_id` is 0.** The raw specialty is in
>   `specialty_source_value`.
> - A few race, ethnicity and enrollment values map to **custom concept
>   ids** above 2 000 000 000.

## After converting

Run an OHDSI Data Quality Dashboard or Achilles pass, and check that the
`CDM_SOURCE` row describes your data and vocabulary version.
