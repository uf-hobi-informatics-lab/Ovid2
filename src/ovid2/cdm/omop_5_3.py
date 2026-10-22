"""OMOP CDM v5.3 handle.

Registers ``omop_5_3`` in :mod:`ovid2.cdm.registry`. This is the target
CDM for the PCORnet->OMOP conversion pipeline (``pcornet_to_omop``).

The handle is materialised from YAML under
``src/ovid2/resources/cdms/omop_5_3/`` via
:func:`ovid2.cdm.yaml_loader.load_cdm`. The mechanical per-table field
lists were generated from the legacy converter's authoritative
``SF_SCHEMAS`` StructType definitions
(the legacy ``pcornetcdm_to_omop_conversion/settings.py``) by
``scripts/gen_omop_cdm_yaml.py``.

Concept-id resolution reuses the same ``concept`` /
``concept_relationship`` shared views the PCORnet CDM declares, so no
new vocabulary material is required.
"""

from __future__ import annotations

import logging

from ovid2.cdm.handle import CdmHandle
from ovid2.cdm.registry import register
from ovid2.cdm.yaml_loader import load_cdm

logger = logging.getLogger(__name__)


OMOP_5_3: CdmHandle = load_cdm("omop_5_3")


def _register_default() -> None:
    """Idempotently register the OMOP 5.3 handle."""
    try:
        register(OMOP_5_3)
    except ValueError:
        # Already registered — safe in reload/test scenarios.
        pass


_register_default()
