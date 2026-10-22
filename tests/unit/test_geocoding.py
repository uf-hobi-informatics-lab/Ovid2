"""Unit tests for ``ovid2.utils.geocoding``.

Covers the static FIPS→state table, the county-suffix stripper, and the
empty-map fallback (when a material lookup file is absent the builder
returns an empty dict and ``_map_expr`` produces a valid, all-NULL map so
downstream ``coalesce`` fallbacks still work).
"""

from __future__ import annotations

import pytest

from ovid2.utils import geocoding


def test_static_fips_to_state() -> None:
    m = geocoding.state_abbrev_from_fips()
    assert m["12"] == "FL"
    assert m["36"] == "NY"
    assert m["06"] == "CA"
    assert m["72"] == "PR"


def test_county_suffix_stripper() -> None:
    assert geocoding._strip_county_suffix("Alachua County") == "Alachua"
    assert geocoding._strip_county_suffix("St. Bernard Parish") == "St. Bernard"
    assert geocoding._strip_county_suffix("Juneau City and Borough") == "Juneau"
    assert geocoding._strip_county_suffix("Baltimore city") == "Baltimore"
    assert geocoding._strip_county_suffix("NoSuffix") == "NoSuffix"


def test_missing_material_files_yield_empty_maps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent lookup files -> empty dicts (not an exception)."""
    from ovid2.config import settings

    geocoding.state_from_zip.cache_clear()
    geocoding.zcta_from_zip.cache_clear()
    geocoding._county_fips_from_zip.cache_clear()
    geocoding.county_name_from_fips.cache_clear()
    geocoding.county_from_zip.cache_clear()

    monkeypatch.setattr(settings, "RUCA_ZIPCODE_XLSX_PATH", settings.MATERIAL_DIR / "nope.xlsx")
    monkeypatch.setattr(settings, "RUCA_TRACT_XLSX_PATH", settings.MATERIAL_DIR / "nope.xlsx")
    monkeypatch.setattr(settings, "GEOCODING_TABLE_PATH", settings.MATERIAL_DIR / "nope.csv")

    assert geocoding.state_from_zip() == {}
    assert geocoding.zcta_from_zip() == {}
    assert geocoding.county_name_from_fips() == {}
    assert geocoding.county_from_zip() == {}

    # clean up caches so other tests re-read real paths
    geocoding.state_from_zip.cache_clear()
    geocoding.zcta_from_zip.cache_clear()
    geocoding._county_fips_from_zip.cache_clear()
    geocoding.county_name_from_fips.cache_clear()
    geocoding.county_from_zip.cache_clear()


def test_map_expr_empty_is_valid(spark: object) -> None:
    """An empty mapping builds a valid create_map whose lookups are NULL."""
    from pyspark.sql.functions import col, lit

    session = spark  # type: ignore[assignment]
    df = session.createDataFrame([("12",)], ["fips"])  # type: ignore[attr-defined]
    empty = geocoding._map_expr({})
    non_empty = geocoding._map_expr({"12": "FL"})
    out = df.select(
        empty[col("fips")].alias("empty_lookup"),
        non_empty[col("fips")].alias("real_lookup"),
        lit("ok").alias("sentinel"),
    ).collect()[0]
    assert out["empty_lookup"] is None
    assert out["real_lookup"] == "FL"
    assert out["sentinel"] == "ok"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
