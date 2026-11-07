"""Ovid2 pipeline-family CDM: nursing (legacy ``nursing_mappers`` family).

Materialised from YAML under ``src/ovid2/resources/cdms/ovid2_pipeline_nursing_v1/``,
generated from the legacy ``mapping_scripts/nursing_mappers/tables_specs.csv``
by ``scripts/legacy_port/gen_legacy_cdms.py``. Field order is the legacy
column order. CDM-default mapper specs (ported legacy mappers) live in the
same tree under ``mappers/``.
"""

from __future__ import annotations

import contextlib

from ovid2.cdm.handle import CdmHandle
from ovid2.cdm.registry import register
from ovid2.cdm.yaml_loader import load_cdm

NURSING_V1: CdmHandle = load_cdm("ovid2_pipeline_nursing_v1")


def _register_default() -> None:
    """Idempotently register the handle."""
    with contextlib.suppress(ValueError):
        register(NURSING_V1)


_register_default()
