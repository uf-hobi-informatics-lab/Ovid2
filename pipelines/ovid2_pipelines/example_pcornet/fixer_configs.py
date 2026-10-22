"""example_pcornet pipeline fixer chain configuration.

Defines the ordered list of fixes to apply per table during the fix
transformation stage.  Each entry maps to a common fixer class in
``transformations.fixers.common.registry.COMMON_FIXES``.

Keys in ``FIXER_CHAINS``:
    - Per-table keys (e.g. ``"encounter"``) list fixes applied to that table.

``MULTI_TABLE_FIXES`` lists fixes that operate across multiple tables
(e.g. ``loinc_mover``) and run after all per-table fixes.

"""

from __future__ import annotations

from typing import Any

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
        {"fix": "providerid_orphans", "src1": "provider", "src2": "PROVIDERID"},
        {"fix": "replication", "src1": "encounter"},
        {"fix": "delete_when_null", "src1": "DX"},
    ],
    "procedures": [
        {"fix": "patid_orphans", "src1": "demographic"},
        {"fix": "encounterid_orphans", "src1": "encounter"},
        {"fix": "providerid_orphans", "src1": "provider", "src2": "PROVIDERID"},
        {"fix": "replication", "src1": "encounter"},
        {"fix": "delete_when_null", "src1": "PX"},
        # Original: illogical_dates_more_than_5_days_fix (custom) — no exact
        # common equivalent; closest are illogical_admit_date /
        # illogical_discharge_date but those require ADMIT_DATE context.
        {"fix": "delete_when_null", "src1": "PROCEDURESID"},
        {"fix": "delete_when_null", "src1": "PX"},
        # Original: invalid_px_type_ch_fix (custom) — no common equivalent yet.
    ],
    "vital": [
        {"fix": "patid_orphans", "src1": "demographic"},
        {"fix": "encounterid_orphans", "src1": "encounter"},
    ],
    "lab_result_cm": [
        {"fix": "patid_orphans", "src1": "demographic"},
        {"fix": "encounterid_orphans", "src1": "encounter"},
        {"fix": "list_remover", "src1": "loinc_list", "src2": "LAB_LOINC"},
        {"fix": "patterns_remover", "src1": "patterns_list", "src2": "RAW_LAB_NAME"},
    ],
    "prescribing": [
        {"fix": "patid_orphans", "src1": "demographic"},
        {"fix": "encounterid_orphans", "src1": "encounter"},
        {"fix": "providerid_orphans", "src1": "provider", "src2": "RX_PROVIDERID"},
    ],
    "dispensing": [
        {"fix": "patid_orphans", "src1": "demographic"},
        {"fix": "delete_when_null", "src1": "NDC"},
    ],
    "condition": [
        {"fix": "patid_orphans", "src1": "demographic"},
        {"fix": "encounterid_orphans", "src1": "encounter"},
        {"fix": "delete_when_null", "src1": "CONDITION"},
    ],
    "death": [
        {"fix": "patid_orphans", "src1": "demographic"},
    ],
    "death_cause": [
        {"fix": "patid_orphans", "src1": "demographic"},
        {"fix": "delete_when_null", "src1": "DEATH_CAUSE"},
    ],
    "enrollment": [
        {"fix": "patid_orphans", "src1": "demographic"},
    ],
    "med_admin": [
        {"fix": "patid_orphans", "src1": "demographic"},
        {"fix": "encounterid_orphans", "src1": "encounter"},
        {"fix": "providerid_orphans", "src1": "provider", "src2": "MEDADMIN_PROVIDERID"},
        {"fix": "copy_when_null", "src1": "MEDADMIN_START_DATE", "src2": "MEDADMIN_STOP_DATE"},
    ],
    "obs_clin": [
        {"fix": "patid_orphans", "src1": "demographic"},
        {"fix": "encounterid_orphans", "src1": "encounter"},
        {"fix": "providerid_orphans", "src1": "provider", "src2": "OBSCLIN_PROVIDERID"},
        {"fix": "list_remover", "src1": "loinc_list", "src2": "OBSCLIN_CODE"},
        {"fix": "list_remover", "src1": "snomed_pii_list", "src2": "OBSCLIN_CODE"},
        {"fix": "list_remover", "src1": "snomed_pii_list", "src2": "RAW_OBSCLIN_CODE"},
    ],
    "obs_gen": [
        {"fix": "patid_orphans", "src1": "demographic"},
        {"fix": "encounterid_orphans", "src1": "encounter"},
        {"fix": "providerid_orphans", "src1": "provider", "src2": "OBSGEN_PROVIDERID"},
        {"fix": "list_remover", "src1": "loinc_list", "src2": "OBSGEN_CODE"},
        {"fix": "list_remover", "src1": "snomed_pii_list", "src2": "OBSGEN_CODE"},
        {"fix": "list_remover", "src1": "snomed_pii_list", "src2": "RAW_OBSGEN_CODE"},
    ],
    "immunization": [
        {"fix": "patid_orphans", "src1": "demographic"},
        {"fix": "encounterid_orphans", "src1": "encounter"},
        {"fix": "providerid_orphans", "src1": "provider", "src2": "VX_PROVIDERID"},
        {"fix": "delete_when_null", "src1": "VX_CODE"},
    ],
    "hash_token": [
        {"fix": "patid_orphans", "src1": "demographic"},
    ],
    "pro_cm": [
        {"fix": "patid_orphans", "src1": "demographic"},
    ],
    "lds_address_history": [
        {"fix": "patid_orphans", "src1": "demographic"},
    ],
    "private_address_history": [
        {"fix": "patid_orphans", "src1": "demographic"},
    ],
    # Tables with no active fixes (pass-through)
    "harvest": [],
    "provider": [],
    "private_address_geocode": [],
}

MULTI_TABLE_FIXES: list[dict[str, Any]] = [
    {"fix": "loinc_mover", "version": "v2"},
]
"""Fixes that operate across multiple tables simultaneously.

Applied after all per-table FIXER_CHAINS have been processed.
"""
