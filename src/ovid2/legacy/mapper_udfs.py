"""Named helper functions used by ported legacy mappers (``legacy_select`` ``udf:``).

Each legacy mapper-local Python UDF is reproduced here once and referenced
by name from mapper YAML. Keep the set closed and documented — a new entry
is a framework change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ovid2.mappers.base import MapOpContext


def _first_non_null(*cols: Any) -> Any:
    """Legacy ``get_state_fips_from_geocoding`` / ``get_countyFips_from_geocoding``.

    Both return the ZIP9-derived value when not null, else the ZIP5 one —
    i.e. ``coalesce``.
    """
    from ovid2.core.engines.dsl import coalesce

    return coalesce(*cols)


#: Name → factory ``(ctx) -> callable(*columns) -> Column``.
MAPPER_UDFS: dict[str, Any] = {
    "get_state_fips_from_geocoding": lambda ctx: _first_non_null,
    "get_countyFips_from_geocoding": lambda ctx: _first_non_null,
}


def mapper_udf(name: str, ctx: MapOpContext) -> Any:
    """Return the column function registered as ``name``.

    Raises:
        ValueError: When ``name`` is not registered.
    """
    factory = MAPPER_UDFS.get(name)
    if factory is None:
        raise ValueError(f"legacy_select: unknown udf '{name}'. Registered: {sorted(MAPPER_UDFS)}.")
    return factory(ctx)
