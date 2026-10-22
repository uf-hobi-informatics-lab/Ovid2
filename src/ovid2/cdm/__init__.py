"""CDM registry — one central catalogue of Common Data Models.

Import this package to force every bundled CDM to register itself with
:mod:`ovid2.cdm.registry`. Callers should use only the public API:

- :func:`get_cdm` — return a :class:`CdmHandle` by name (defaults to
  ``pcornet_7_0``).
- :func:`register` — register a custom CDM (third-party plugins).
- :func:`registered_cdms` — list currently-registered CDM names.

The ETL engine (``core/pipeline.py``, ``core/loaders.py``,
``transformations/deduplicators/base.py``) reads CDM information *only*
through this API. No engine code ever imports a specific CDM module.
That is the P10 CDM/ETL independence contract.
"""

from __future__ import annotations

from ovid2.cdm.handle import CdmHandle, IdColumn, SharedViewSpec
from ovid2.cdm.registry import (
    DEFAULT_CDM,
    GENERIC_CDM_LABELS,
    get_cdm,
    register,
    registered_cdms,
    resolve_cdm_for_pipeline,
)
from ovid2.schema.cdm import CdmField  # re-export for convenience

# Import each bundled CDM module for its side effect (self-registration).
from ovid2.cdm import nursing_v1 as _nursing_v1  # noqa: F401
from ovid2.cdm import omop_5_3 as _omop_5_3  # noqa: F401
from ovid2.cdm import pcornet_7_0 as _pcornet_7_0  # noqa: F401
from ovid2.cdm import tumor_v1 as _tumor_v1  # noqa: F401
from ovid2.cdm import veritas_v1 as _veritas_v1  # noqa: F401

__all__ = [
    "CdmHandle",
    "CdmField",
    "IdColumn",
    "SharedViewSpec",
    "DEFAULT_CDM",
    "GENERIC_CDM_LABELS",
    "get_cdm",
    "register",
    "registered_cdms",
    "resolve_cdm_for_pipeline",
]
