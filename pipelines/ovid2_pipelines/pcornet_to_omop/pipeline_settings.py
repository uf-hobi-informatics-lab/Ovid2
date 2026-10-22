"""Pipeline settings for the PCORnet -> OMOP converter.

Binds to the OMOP 5.3 CDM so ``resolve_cdm_for_pipeline`` returns the
OMOP handle and the ETL table loop iterates OMOP output tables.
"""

from __future__ import annotations

# Target CDM: the pipeline reads PCORnet input and writes OMOP tables.
cdm = "omop_5_3"

# CDM_SOURCE metadata (populates the OMOP cdm_source table in M3).
CDM_SOURCE_NAME = "PCORnet-to-OMOP conversion"
CDM_SOURCE_ABBREVIATION = "PCORNET-OMOP"
CDM_HOLDER = ""
SOURCE_RELEASE_DATE = ""
CDM_RELEASE_DATE = ""
CDM_VERSION = "5.3"
VOCABULARY_VERSION = ""

# No source-side merge; each run converts one PCORnet batch.
DEFAULT_UPLOAD_TYPE = "no_merge"
