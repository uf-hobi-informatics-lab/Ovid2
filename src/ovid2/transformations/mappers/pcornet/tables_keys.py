"""PCORnet table configuration — composite keys for deduplication.

.. deprecated:: P10

    This module has been superseded by
    :data:`ovid2.cdm.pcornet_7_0.PCORNET_7_0` and the
    :meth:`~ovid2.cdm.handle.CdmHandle.dedup_key_for` API. The
    :data:`COMPOSITE_KEYS` dict below is now a re-export from the CDM
    registry, retained so any legacy code that imports it keeps working.
    New code MUST resolve keys through the CDM handle passed on the
    pipeline context (``context["cdm"]``).

    The re-export loses tuple immutability — values are cast to
    ``list[str]`` to match the pre-P10 shape.
"""

from __future__ import annotations

from ovid2.cdm.registry import get_cdm

_HANDLE = get_cdm("pcornet_7_0")

COMPOSITE_KEYS: dict[str, list[str]] = {
    table: list(keys) for table, keys in _HANDLE.composite_keys.items()
}
