"""Registry of all common fixer classes.

Maps fix name strings to their :class:`~ovid2.transformations.fixers.base.BaseFixer`
subclasses so :class:`~ovid2.transformations.fix.FixTransformer` can look up
a fixer by the string in ``FIXER_CHAINS``.

Usage::

    from ovid2.transformations.fixers.common.registry import COMMON_FIXES

    fixer = COMMON_FIXES["patid_orphans"]()
    result_df = fixer.apply(df, context)
"""

from __future__ import annotations

from ovid2.transformations.fixers.base import BaseFixer
from ovid2.transformations.fixers.common.add_missing_hash_token_patids import (
    AddMissingHashTokenPatidsFixer,
)
from ovid2.transformations.fixers.common.append_to_existing_lds_address_hx import (
    AppendToExistingLdsAddressHxFixer,
)
from ovid2.transformations.fixers.common.copy_admit_date_or_delete import (
    CopyAdmitDateOrDeleteFixer,
)
from ovid2.transformations.fixers.common.copy_when_null import CopyWhenNullFixer
from ovid2.transformations.fixers.common.delete_records_after_death import (
    DeleteRecordsAfterDeathFixer,
)
from ovid2.transformations.fixers.common.delete_records_before_birth import (
    DeleteRecordsBeforeBirthFixer,
)
from ovid2.transformations.fixers.common.delete_when_null import DeleteWhenNullFixer
from ovid2.transformations.fixers.common.delete_when_null_or_fon import (
    DeleteWhenNullOrFonFixer,
)
from ovid2.transformations.fixers.common.encounterid_orphans import (
    EncounteridOrphansFixer,
)
from ovid2.transformations.fixers.common.future_date_to_null import (
    FutureDateToNullFixer,
)
from ovid2.transformations.fixers.common.illogical_admit_date import (
    IllogicalAdmitDateFixer,
)
from ovid2.transformations.fixers.common.illogical_discharge_date import (
    IllogicalDischargeDateFixer,
)
from ovid2.transformations.fixers.common.invalid_to_null import InvalidToNullFixer
from ovid2.transformations.fixers.common.list_remover import ListRemoverFixer
from ovid2.transformations.fixers.common.loinc_mover import LoincMoverFixer
from ovid2.transformations.fixers.common.norm_range_normalization import (
    NormRangeNormalizationFixer,
)
from ovid2.transformations.fixers.common.null_date_to_harvest_refresh import (
    NullDateToHarvestRefreshFixer,
)
from ovid2.transformations.fixers.common.panel_loinc_remover import (
    PanelLoincRemoverFixer,
)
from ovid2.transformations.fixers.common.patid_orphans import PatidOrphansFixer
from ovid2.transformations.fixers.common.patterns_remover import PatternsRemoverFixer
from ovid2.transformations.fixers.common.providerid_orphans import (
    ProvideridOrphansFixer,
)
from ovid2.transformations.fixers.common.race_eth import RaceEthFixer
from ovid2.transformations.fixers.common.replication import ReplicationFixer

COMMON_FIXES: dict[str, type[BaseFixer]] = {
    "add_missing_hash_token_patids": AddMissingHashTokenPatidsFixer,
    "append_to_existing_lds_address_hx": AppendToExistingLdsAddressHxFixer,
    "copy_admit_date_or_delete": CopyAdmitDateOrDeleteFixer,
    "copy_when_null": CopyWhenNullFixer,
    "delete_records_after_death": DeleteRecordsAfterDeathFixer,
    "delete_records_before_birth": DeleteRecordsBeforeBirthFixer,
    "delete_when_null": DeleteWhenNullFixer,
    "delete_when_null_or_fon": DeleteWhenNullOrFonFixer,
    "encounterid_orphans": EncounteridOrphansFixer,
    "future_date_to_null": FutureDateToNullFixer,
    "illogical_admit_date": IllogicalAdmitDateFixer,
    "illogical_discharge_date": IllogicalDischargeDateFixer,
    "invalid_to_null": InvalidToNullFixer,
    "list_remover": ListRemoverFixer,
    "loinc_mover": LoincMoverFixer,
    "norm_range_normalization": NormRangeNormalizationFixer,
    "null_date_to_harvest_refresh": NullDateToHarvestRefreshFixer,
    "panel_loinc_remover": PanelLoincRemoverFixer,
    "patid_orphans": PatidOrphansFixer,
    "patterns_remover": PatternsRemoverFixer,
    "providerid_orphans": ProvideridOrphansFixer,
    "race_eth": RaceEthFixer,
    "replication": ReplicationFixer,
}

# ---------------------------------------------------------------------------
# Named-field aliases (P10)
# ---------------------------------------------------------------------------
# Each entry declares self-documenting names for the legacy ``src1``/``src2``
# positional fields. The :class:`~ovid2.transformations.fix.FixTransformer`
# normalises every config entry into the ``src1`` / ``src2`` shape before
# calling :meth:`BaseFixer.apply`, so existing fixer implementations keep
# working. Contract test ``test_fixer_configs_valid`` validates every
# pipeline ``FIXER_CHAINS`` entry against these aliases.
#
# Meaning legend:
#
# - ``reference_table``: name of another table registered as a Spark
#   temp view (e.g. ``'demographic'``, ``'encounter'``, ``'harvest'``).
# - ``target_column``:   column name in the input DataFrame to inspect.
# - ``list_name``:       basename (without ``.csv``) of a reference file
#   under ``$MATERIAL_DIR/lists/``.
# - ``version``:         version string for stateful fixes (e.g.
#   ``'v1'`` / ``'v2'`` for :class:`LoincMoverFixer`).
FIXER_ALIASES: dict[str, dict[str, str]] = {
    "add_missing_hash_token_patids": {"reference_table": "src1"},
    "append_to_existing_lds_address_hx": {},
    "copy_admit_date_or_delete": {"target_column": "src1"},
    "copy_when_null": {"source_column": "src1", "target_column": "src2"},
    "delete_records_after_death": {"reference_table": "src1", "target_column": "src2"},
    "delete_records_before_birth": {"reference_table": "src1", "target_column": "src2"},
    "delete_when_null": {"target_column": "src1"},
    "delete_when_null_or_fon": {"target_column": "src1"},
    "encounterid_orphans": {"reference_table": "src1", "target_column": "src2"},
    "future_date_to_null": {"target_column": "src1"},
    "illogical_admit_date": {"target_column": "src1"},
    "illogical_discharge_date": {"target_column": "src1"},
    "invalid_to_null": {"target_column": "src1", "valid_values_name": "src2"},
    "list_remover": {"list_name": "src1", "target_column": "src2"},
    "loinc_mover": {"target_column": "src1"},
    "norm_range_normalization": {"target_column": "src1"},
    "null_date_to_harvest_refresh": {"reference_table": "src1", "target_column": "src2"},
    "panel_loinc_remover": {"list_name": "src1", "target_column": "src2"},
    "patid_orphans": {"reference_table": "src1", "target_column": "src2"},
    "patterns_remover": {"list_name": "src1", "target_column": "src2"},
    "providerid_orphans": {"reference_table": "src1", "target_column": "src2"},
    "race_eth": {"race_column": "src1", "hispanic_column": "src2"},
    "replication": {"target_column": "src1"},
}


def normalise_fix_config(fix_config: dict[str, object]) -> dict[str, object]:
    """Rewrite named-field aliases into the legacy ``src1``/``src2`` shape.

    Given a config entry like::

        {"fix": "patid_orphans", "reference_table": "demographic"}

    returns::

        {"fix": "patid_orphans", "reference_table": "demographic",
         "src1": "demographic"}

    The original named fields are preserved for observability. When both
    the alias and the positional key are present, the positional key wins
    and a warning-worthy ambiguity is surfaced to the caller via the
    returned dict retaining both.

    Unknown fixes pass through unchanged. Unknown aliases pass through
    unchanged (per-fixer contract test flags them).
    """
    fix_name = str(fix_config.get("fix", ""))
    if fix_name not in FIXER_ALIASES:
        return dict(fix_config)

    aliases = FIXER_ALIASES[fix_name]
    normalised: dict[str, object] = dict(fix_config)
    for named, positional in aliases.items():
        if named in normalised and positional not in normalised:
            normalised[positional] = normalised[named]
    return normalised


# ---------------------------------------------------------------------------
# CDM-scope classification (P10)
# ---------------------------------------------------------------------------
# Each fixer is classified as either:
#
# - ``'generic'``  — data-driven; works on any CDM whose columns are
#   named at runtime via ``target_column`` / ``reference_table``.
# - ``'<cdm_name>'`` — hardcodes columns, tables, or valuesets specific
#   to that CDM.
#
# Contract test ``test_fixer_cdm_scope`` fails a pipeline whose
# ``FIXER_CHAINS`` names a CDM-scoped fixer that does not match the
# pipeline's declared CDM.
#
# ``race_eth`` — hardcodes PCORnet race codes (01..05, R, OT, UN, NI).
# ``null_date_to_harvest_refresh`` — hardcodes ``spark.table("harvest")``.
# ``add_missing_hash_token_patids`` — only meaningful for PCORnet ``hash_token``.
# ``delete_when_null_or_fon`` — encodes PCORnet flavour-of-null (NI/UN/OT).
# ``loinc_mover`` / ``panel_loinc_remover`` — encode PCORnet EDC DC checks.
FIXER_SCOPES: dict[str, str] = {
    # Generic (CDM-independent) fixers.
    "append_to_existing_lds_address_hx": "generic",
    "copy_admit_date_or_delete": "generic",
    "copy_when_null": "generic",
    "delete_records_after_death": "generic",
    "delete_records_before_birth": "generic",
    "delete_when_null": "generic",
    "encounterid_orphans": "generic",
    "future_date_to_null": "generic",
    "illogical_admit_date": "generic",
    "illogical_discharge_date": "generic",
    "invalid_to_null": "generic",
    "list_remover": "generic",
    "norm_range_normalization": "generic",
    "patid_orphans": "generic",
    "patterns_remover": "generic",
    "providerid_orphans": "generic",
    "replication": "generic",
    # PCORnet-scoped fixers.
    "add_missing_hash_token_patids": "pcornet_7_0",
    "delete_when_null_or_fon": "pcornet_7_0",
    "loinc_mover": "pcornet_7_0",
    "null_date_to_harvest_refresh": "pcornet_7_0",
    "panel_loinc_remover": "pcornet_7_0",
    "race_eth": "pcornet_7_0",
}


def scope_of(fix_name: str) -> str:
    """Return the CDM scope of ``fix_name``, or ``'generic'`` when unknown.

    Unknown fixes are treated as generic so third-party additions do not
    break the contract test until they explicitly declare a scope.
    """
    return FIXER_SCOPES.get(fix_name, "generic")


def is_compatible_with_cdm(fix_name: str, cdm_name: str) -> bool:
    """Return ``True`` when ``fix_name`` is safe to run under ``cdm_name``.

    Generic fixes are compatible with every CDM. CDM-scoped fixes are
    compatible only with their own CDM.
    """
    scope = scope_of(fix_name)
    return scope == "generic" or scope == cdm_name
