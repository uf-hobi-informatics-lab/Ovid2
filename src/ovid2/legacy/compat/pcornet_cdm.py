"""Legacy ``common/pcornet_cdm.py`` shim (``PcornetCDM.get_table_headers``).

Legacy read the header list from ``common/cdm/pcornet_7_0/pcornet_cdm_v70.csv``.
The Ovid2 PCORnet CDM YAML tree carries the same ordered field lists
(imported from the same spec), so the shim reads them from the CDM handle.
"""

from __future__ import annotations

from typing import Any


class PcornetCDM:
    """Legacy ``PcornetCDM`` subset."""

    @classmethod
    def get_table_headers(cls, spark: Any, table_name: str) -> list[str]:
        """Return the ordered PCORnet field names for ``table_name``."""
        del spark
        from ovid2.cdm.registry import get_cdm

        return get_cdm("pcornet_7_0").load_table_columns(table_name.lower())
