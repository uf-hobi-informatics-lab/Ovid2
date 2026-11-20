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

# Import each bundled CDM module for its side effect (self-registration).
from ovid2.cdm import nursing_v1 as _nursing_v1  # noqa: F401
from ovid2.cdm import omop_5_3 as _omop_5_3  # noqa: F401
from ovid2.cdm import pcornet_7_0 as _pcornet_7_0  # noqa: F401
from ovid2.cdm import tumor_v1 as _tumor_v1  # noqa: F401
from ovid2.cdm import veritas_v1 as _veritas_v1  # noqa: F401
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


def _register_pack_cdms() -> None:
    """Register every CDM provided by an active (non-built-in) domain pack.

    A broken pack CDM is logged and skipped — it never stops the core
    CDMs from loading. Tier-neutral: reads YAML only.
    """
    import logging

    from ovid2.cdm.yaml_loader import CdmYamlError, load_cdm
    from ovid2.packs import active_packs

    log = logging.getLogger(__name__)
    for info in active_packs():
        if info.builtin or info.manifest is None:
            continue
        for name in info.manifest.cdms:
            if name in registered_cdms():
                continue
            try:
                register(load_cdm(name))
            except (CdmYamlError, ValueError, KeyError, OSError) as exc:
                log.warning("pack %s: CDM %s not registered: %s", info.id, name, exc)


_register_pack_cdms()

__all__ = [
    "DEFAULT_CDM",
    "GENERIC_CDM_LABELS",
    "CdmField",
    "CdmHandle",
    "IdColumn",
    "SharedViewSpec",
    "get_cdm",
    "register",
    "registered_cdms",
    "resolve_cdm_for_pipeline",
]
