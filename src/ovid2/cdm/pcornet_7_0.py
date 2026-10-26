"""PCORnet CDM v7.0 handle.

Registers ``pcornet_7_0`` as the default CDM in
:mod:`ovid2.cdm.registry`.

As of phase C1 the handle is materialised from YAML under
``src/ovid2/resources/cdms/pcornet_7_0/`` via
:func:`ovid2.cdm.yaml_loader.load_cdm`. See
the adr-cdm-yaml-and-route-kinds design note for the
migration rationale.

The hardcoded dicts that used to live in this module (composite keys,
id_columns, reference_tables, flavour_of_null, shared_views) are now
authored in ``cdm.yaml``. The ``ovid2 tools import-cdm`` command
re-generates the mechanical per-table and per-valueset files without
touching the curated top-level metadata.
"""

from __future__ import annotations

import contextlib
import logging

from ovid2.cdm.handle import CdmHandle
from ovid2.cdm.registry import register
from ovid2.cdm.yaml_loader import load_cdm

logger = logging.getLogger(__name__)


PCORNET_7_0: CdmHandle = load_cdm("pcornet_7_0")


def _register_default() -> None:
    """Idempotently register the PCORnet handle."""
    # Already registered — safe in reload/test scenarios.
    with contextlib.suppress(ValueError):
        register(PCORNET_7_0)


_register_default()
