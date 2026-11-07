"""Ovid2 pipeline-family CDM: veritas (legacy ``veritas_mappers`` family).

Materialised from YAML under ``src/ovid2/resources/cdms/ovid2_pipeline_veritas_v1/``,
generated from the legacy ``mapping_scripts/veritas_mappers/tables_specs.csv``
by ``scripts/legacy_port/gen_legacy_cdms.py``. Field order is the legacy
column order. CDM-default mapper specs (ported legacy mappers) live in the
same tree under ``mappers/``.
"""

from __future__ import annotations

import contextlib

from ovid2.cdm.handle import CdmHandle
from ovid2.cdm.registry import register
from ovid2.cdm.yaml_loader import load_cdm

VERITAS_V1: CdmHandle = load_cdm("ovid2_pipeline_veritas_v1")


def _register_default() -> None:
    """Idempotently register the handle."""
    with contextlib.suppress(ValueError):
        register(VERITAS_V1)


_register_default()
