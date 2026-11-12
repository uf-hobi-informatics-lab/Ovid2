---
title: Data & data models
summary: For researchers — what Ovid2's output contains, how it was produced, and how to judge whether it is fit for your question.
audience: [researcher]
---

This chapter is for people who **use** the data Ovid2 produces:
analysts, investigators, data stewards and reviewers. You don't need to
run Ovid2 to read it.

<div class="cards">
  <a class="card" href="pcornet.md"><strong>PCORnet CDM 7.0</strong><span>The tables, keys and valuesets of the default output model.</span></a>
  <a class="card" href="omop.md"><strong>OMOP CDM 5.3</strong><span>The OHDSI model, and how Ovid2 fills it.</span></a>
  <a class="card" href="pcornet-to-omop.md"><strong>PCORnet → OMOP</strong><span>Table-by-table conversion rules and known limitations.</span></a>
  <a class="card" href="vocabularies.md"><strong>Vocabularies</strong><span>ICD, LOINC, SNOMED, RxNorm, NDC and OMOP concepts.</span></a>
  <a class="card" href="data-lineage.md"><strong>Data lineage</strong><span>Trace any value back to its source row and rule.</span></a>
  <a class="card" href="reading-health-reports.md"><strong>Reading a health report</strong><span>Interpret EDC checks and health panels.</span></a>
  <a class="card" href="report-studio.md"><strong>Report Studio</strong><span>Design, share and export reports from your runs.</span></a>
  <a class="card" href="privacy.md"><strong>Privacy & identifiers</strong><span>Hashed ids, tokens, and what never leaves the executor.</span></a>
</div>

## Four questions to ask of any delivery

1. **Which model and version?** The output's CDM is recorded in the run
   and, for PCORnet, in `HARVEST.CDM_VERSION`. → [PCORnet](pcornet.md),
   [OMOP](omop.md)
2. **Which rules produced it?** Every table comes from a declarative
   formatter, mapper and fixer chain that you can read. →
   [Data lineage](data-lineage.md)
3. **What was corrected or removed?** Every fixer logs an event per table.
   → [Data lineage](data-lineage.md#what-the-fixers-changed)
4. **How good is it?** Run a health check and read the report. →
   [Reading a health report](reading-health-reports.md); turn it into a
   shareable, exportable report in the [Report Studio](report-studio.md)

> [!TIP]
> Every CDM can be browsed in the web panel under **Configure → CDMs**:
> tables, fields, types, keys and allowed values. The generated
> [CDM reference](../10-reference/cdms.md) shows the same information.
