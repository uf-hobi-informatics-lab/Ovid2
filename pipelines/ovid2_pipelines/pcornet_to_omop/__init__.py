"""PCORnet -> OMOP conversion pipeline package.

Unlike the other pipeline packages (which format a *source* feed *into*
the PCORnet CDM), ``pcornet_to_omop`` reads PCORnet CDM tables and emits
OMOP CDM 5.3 tables. It binds to the ``omop_5_3`` CDM
(``pipeline_settings.cdm``) so the pipeline's table loop iterates OMOP
output tables.

This is the Ovid2 port of the legacy PySpark converter at
the legacy ``pcornetcdm_to_omop_conversion`` project. Team decisions baked in:

* PCORnet primary keys are reused verbatim as OMOP ids (no id-mapping
  step) — e.g. ``person_id = PATID``, ``visit_occurrence_id =
  ENCOUNTERID``.
* Concept-id resolution reuses the existing ``concept`` /
  ``concept_relationship`` shared views (no vocabulary material is
  generated or uploaded).
* No persisted intermediate ``common`` staging table — each OMOP
  formatter reads its PCORnet source(s) directly.
"""

from __future__ import annotations
