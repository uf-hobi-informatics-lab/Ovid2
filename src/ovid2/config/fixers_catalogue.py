"""Web-safe catalogue of the common fixers (mirror of the fixer registry).

The web tier and the MCP tier may not import ovid2.transformations
(``tests/contract/test_web_no_pipeline_imports.py``), yet the pipeline
builder needs the fixer menu — names, one-line summaries, CDM scope and
the named-field aliases for ``src1`` / ``src2``. This module is that
copy. ``tests/contract/test_fixers_catalogue_synchronised.py`` fails
when it drifts from
:mod:`ovid2.transformations.fixers.common.registry`.

Alias meaning (see the registry): ``reference_table`` — another CDM
table registered earlier in the run; ``target_column`` — a column of the
fixed table; ``list_name`` / ``valid_values_name`` — basename of
``$OVID2_MATERIAL_DIR/lists/<name>.csv``.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "CANONICAL_KEYS",
    "FIXER_CATALOGUE",
    "FIXER_COLUMN_ROLES",
    "LEGACY_FIX_NAMES",
    "list_fixers",
    "native_fix_name",
]

#: Keys every fix entry may carry regardless of aliases.
CANONICAL_KEYS: frozenset[str] = frozenset({"fix", "src1", "src2", "table", "version"})

FIXER_CATALOGUE: dict[str, dict[str, Any]] = {
    "add_missing_hash_token_patids": {
        "summary": "Add stub hash_token rows for PATIDs that exist in demographic but not hash_token.",
        "scope": "pcornet_7_0",
        "aliases": {"reference_table": "src1"},
    },
    "append_to_existing_lds_address_hx": {
        "summary": "Append existing LDS address history from Snowflake to the current batch.",
        "scope": "generic",
        "aliases": {},
    },
    "copy_admit_date_or_delete": {
        "summary": "Backfill null date fields with ADMIT_DATE from the encounter table.",
        "scope": "generic",
        "aliases": {"target_column": "src1"},
    },
    "copy_when_null": {
        "summary": "Copy a source column value into the target column when the target is null.",
        "scope": "generic",
        "aliases": {"source_column": "src1", "target_column": "src2"},
    },
    "delete_records_after_death": {
        "summary": "Remove records whose date field is later than the patient's death date.",
        "scope": "generic",
        "aliases": {"reference_table": "src1", "target_column": "src2"},
    },
    "delete_records_before_birth": {
        "summary": "Remove records whose date field is earlier than the patient's birth date.",
        "scope": "generic",
        "aliases": {"reference_table": "src1", "target_column": "src2"},
    },
    "delete_when_null": {
        "summary": "Remove rows where a specified field is null.",
        "scope": "generic",
        "aliases": {"target_column": "src1"},
    },
    "delete_when_null_or_fon": {
        "summary": "Remove rows where a specified field is null, empty, or a flavor of null.",
        "scope": "pcornet_7_0",
        "aliases": {"target_column": "src1"},
    },
    "encounterid_orphans": {
        "summary": "Null out ENCOUNTERID values that have no match in the encounter table.",
        "scope": "generic",
        "aliases": {"reference_table": "src1", "target_column": "src2"},
    },
    "future_date_to_null": {
        "summary": "Null out date values that fall after the harvest refresh date.",
        "scope": "generic",
        "aliases": {"target_column": "src1"},
    },
    "illogical_admit_date": {
        "summary": "Replace date values that are more than 5 days before ADMIT_DATE.",
        "scope": "generic",
        "aliases": {"target_column": "src1"},
    },
    "illogical_discharge_date": {
        "summary": "Replace date values that are more than 5 days after DISCHARGE_DATE.",
        "scope": "generic",
        "aliases": {"target_column": "src1"},
    },
    "invalid_to_null": {
        "summary": "Remove rows where the specified field is null.",
        "scope": "generic",
        "aliases": {"target_column": "src1", "valid_values_name": "src2"},
    },
    "list_remover": {
        "summary": "Remove rows whose column value is present in a reference list.",
        "scope": "generic",
        "aliases": {"list_name": "src1", "target_column": "src2"},
    },
    "loinc_mover": {
        "summary": "Remove rows whose LOINC code belongs in a different CDM table.",
        "scope": "pcornet_7_0",
        "aliases": {"target_column": "src1"},
    },
    "norm_range_normalization": {
        "summary": "Normalize NORM_RANGE and NORM_MODIFIER fields in lab_result_cm.",
        "scope": "generic",
        "aliases": {"target_column": "src1"},
    },
    "null_date_to_harvest_refresh": {
        "summary": "Backfill null date values with the corresponding harvest refresh date.",
        "scope": "pcornet_7_0",
        "aliases": {"reference_table": "src1", "target_column": "src2"},
    },
    "panel_loinc_remover": {
        "summary": "Remove rows with panel-type LOINC codes from the DataFrame.",
        "scope": "pcornet_7_0",
        "aliases": {"list_name": "src1", "target_column": "src2"},
    },
    "patid_orphans": {
        "summary": "Remove rows whose PATID is not present in a reference table.",
        "scope": "generic",
        "aliases": {"reference_table": "src1", "target_column": "src2"},
    },
    "patterns_remover": {
        "summary": "Remove rows where a column matches any pattern from a reference list.",
        "scope": "generic",
        "aliases": {"list_name": "src1", "target_column": "src2"},
    },
    "providerid_orphans": {
        "summary": "Null out provider ID values that have no match in the provider table.",
        "scope": "generic",
        "aliases": {"reference_table": "src1", "target_column": "src2"},
    },
    "race_eth": {
        "summary": "Derive seven RACE_ETH_* columns from RACE and HISPANIC values.",
        "scope": "pcornet_7_0",
        "aliases": {"race_column": "src1", "hispanic_column": "src2"},
    },
    "replication": {
        "summary": "Copy ADMIT_DATE and ENC_TYPE from the encounter table into the main table.",
        "scope": "generic",
        "aliases": {"target_column": "src1"},
    },
}


#: Which columns each fixer touches, resolved against the *normalised*
#: ``src1`` / ``src2`` slots (grammar: :mod:`ovid2.config.column_roles`;
#: UPPERCASE tokens are literal columns). Derived from each fixer's
#: ``apply`` body — the ground truth of what runs — not from
#: :data:`FIXER_CATALOGUE` aliases. ``test_column_roles_complete.py`` pins
#: one role per fixer.
FIXER_COLUMN_ROLES: dict[str, dict[str, Any]] = {
    "add_missing_hash_token_patids": {"scope": "rows", "reads": ["PATID"], "tables": ["src1"]},
    "append_to_existing_lds_address_hx": {
        "scope": "rows",
        "writes": ["ADDRESS_PERIOD_START", "ADDRESS_PERIOD_END"],
    },
    "copy_admit_date_or_delete": {
        "scope": "field",
        "inplace": ["src1:csv"],
        "reads": ["ENCOUNTERID"],
        "tables": ["src2"],
        "filters_rows": True,
    },
    "copy_when_null": {"scope": "field", "inplace": ["src1"], "reads": ["src2"]},
    "delete_records_after_death": {
        "scope": "field",
        "reads": ["src1", "PATID"],
        "tables": ["src2"],
        "filters_rows": True,
    },
    "delete_records_before_birth": {
        "scope": "field",
        "reads": ["src1", "PATID"],
        "tables": ["src2"],
        "filters_rows": True,
    },
    "delete_when_null": {"scope": "field", "reads": ["src1"], "filters_rows": True},
    "delete_when_null_or_fon": {"scope": "field", "reads": ["src1"], "filters_rows": True},
    "encounterid_orphans": {"scope": "field", "inplace": ["ENCOUNTERID"], "tables": ["src1"]},
    "future_date_to_null": {"scope": "field", "inplace": ["src1"]},
    "illogical_admit_date": {"scope": "field", "inplace": ["src1"], "reads": ["ADMIT_DATE"]},
    "illogical_discharge_date": {
        "scope": "field",
        "inplace": ["src1"],
        "reads": ["ENCOUNTERID"],
        "tables": ["src2"],
    },
    "invalid_to_null": {"scope": "field", "reads": ["src1"], "filters_rows": True},
    "list_remover": {"scope": "field", "reads": ["src2"], "filters_rows": True},
    "loinc_mover": {
        "scope": "field",
        "by_table": {
            "lab_result_cm": "LAB_LOINC",
            "obs_clin": "OBSCLIN_CODE",
            "obs_gen": "OBSGEN_CODE",
        },
        "filters_rows": True,
    },
    "norm_range_normalization": {
        "scope": "field",
        "inplace": ["NORM_MODIFIER_LOW", "NORM_MODIFIER_HIGH", "NORM_RANGE_LOW", "NORM_RANGE_HIGH"],
    },
    "null_date_to_harvest_refresh": {"scope": "field", "inplace": ["src1"]},
    "panel_loinc_remover": {"scope": "field", "reads": ["src2:csv"], "filters_rows": True},
    "patid_orphans": {
        "scope": "field",
        "reads": ["src2|PATID"],
        "tables": ["src1"],
        "filters_rows": True,
    },
    "patterns_remover": {"scope": "field", "reads": ["src2"], "filters_rows": True},
    "providerid_orphans": {"scope": "field", "inplace": ["src2"], "tables": ["src1"]},
    "race_eth": {
        "scope": "field",
        "reads": ["src1|RACE", "src2|HISPANIC"],
        "writes": [
            "RACE_ETH_AI_AN",
            "RACE_ETH_ASIAN",
            "RACE_ETH_BLACK",
            "RACE_ETH_NH_PI",
            "RACE_ETH_WHITE",
            "RACE_ETH_HISPANIC",
            "RACE_ETH_MISSING",
        ],
    },
    "replication": {
        "scope": "field",
        "reads": ["ENCOUNTERID"],
        "writes": ["ADMIT_DATE", "ENC_TYPE"],
        "tables": ["src1"],
    },
}

#: Legacy (vendored) fixer names whose stem differs from the Ovid2 fixer.
LEGACY_FIX_NAMES: dict[str, str] = {
    "illogical_admit_date_more_than_5_days_fix": "illogical_admit_date",
    "illogical_discharge_date_more_than_5_days_fix": "illogical_discharge_date",
    "delete_when_null_or_flavor_of_null_fix": "delete_when_null_or_fon",
    "inavalid_to_null_fix": "invalid_to_null",
}


def native_fix_name(legacy_name: str) -> str | None:
    """Return the Ovid2 fixer a vendored legacy ``fix_name`` corresponds to."""
    if legacy_name in LEGACY_FIX_NAMES:
        return LEGACY_FIX_NAMES[legacy_name]
    stem = legacy_name.removesuffix("_fix")
    return stem if stem in FIXER_CATALOGUE else None


def list_fixers(cdm: str | None = None) -> list[dict[str, Any]]:
    """Return catalogue entries (``name`` added), optionally CDM-filtered."""
    out: list[dict[str, Any]] = []
    for name, entry in sorted(FIXER_CATALOGUE.items()):
        if cdm and entry["scope"] not in ("generic", cdm):
            continue
        out.append({"name": name, **entry})
    return out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.config.fixers_catalogue is a library module.")
