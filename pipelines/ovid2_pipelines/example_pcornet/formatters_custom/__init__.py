"""Hand-written formatters for example_pcornet (adapter: custom escape hatch).

The declarative YAML specs under ``formatters/`` cover every mechanical
pass-through. Formatters that need cross-DataFrame operations,
aggregations, joins with reference tables, or eager evaluation live here.

Currently:

- :class:`HarvestFormatter` — derives REFRESH dates from max admit_date.
- :class:`DeathCauseFormatter` — explodes comma-separated DEATH_CAUSE.
- :class:`PrivateAddressGeocodeFormatter` — joins with the geocoding CSV.
"""

from __future__ import annotations

__all__ = [
    "DeathCauseFormatter",
    "HarvestFormatter",
    "PrivateAddressGeocodeFormatter",
]
