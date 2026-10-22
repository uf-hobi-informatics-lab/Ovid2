"""Geocoding lookup expressions for the ``private_address_geocode`` formatters.

Ported from the FIPS / ZIP lookup builders in
the legacy Ovid ``common/commonFunctions.py`` (``get_state_abbrev_from_fips``,
``get_countyName_from_fips``, ``get_state_from_zip``, ``get_county_from_zip``,
``get_zcta_from_zip``). Each ``*_map_expr()`` returns a Spark ``create_map``
Column that a formatter indexes by key, e.g.::

    from ovid2.utils.geocoding import fips_state_abbrev_map_expr
    df.select(fips_state_abbrev_map_expr()[col("GEOCODE_STATE")].alias("GEOCODE_STATE"))

The dictionaries are built host-side from the material lookup files
(``RUCA-codes-2020-*.xlsx`` + ``FULL_GEOCODING_202603_16.csv`` under
``settings.COMMON_LISTS_DIR``) and cached per-process. Building a broadcast
``create_map`` from a small Python dict keeps the join fully lazy — no
``.collect()`` on the address data itself.

The static state-FIPS→abbreviation table is inlined (it never changes). The
ZIP/county/ZCTA maps read the material files lazily on first use; when those
files are absent (e.g. a dev host without ``material/``) the builder returns
an empty map so a ``map[...]`` lookup yields ``NULL`` and the formatter's
``coalesce`` fallbacks still produce well-formed output.
"""

from __future__ import annotations

import csv
import logging
from collections import Counter
from functools import lru_cache
from itertools import chain
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyspark.sql import Column

logger = logging.getLogger(__name__)

# Static 2-digit state FIPS -> 2-letter abbreviation (never changes).
_FIPS_TO_STATE_ABBREV: dict[str, str] = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA",
    "08": "CO", "09": "CT", "10": "DE", "11": "DC", "12": "FL",
    "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN",
    "19": "IA", "20": "KS", "21": "KY", "22": "LA", "23": "ME",
    "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS",
    "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND",
    "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
    "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
    "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI",
    "56": "WY", "60": "AS", "66": "GU", "69": "MP", "72": "PR",
    "78": "VI",
}

_COUNTY_SUFFIXES = (
    " City and Borough", " Census Area", " Municipality",
    " Municipio", " Borough", " Parish", " County", " city",
)


def _strip_county_suffix(name: str) -> str:
    for suffix in _COUNTY_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


# ---------------------------------------------------------------------------
# Dictionary builders (host-side, cached)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def state_abbrev_from_fips() -> dict[str, str]:
    """Return the static 2-digit-FIPS → 2-letter-state-abbreviation map."""
    return dict(_FIPS_TO_STATE_ABBREV)


@lru_cache(maxsize=1)
def county_name_from_fips() -> dict[str, str]:
    """Return 5-digit county-FIPS → county name from the RUCA Tract workbook."""
    from ovid2.config.settings import RUCA_TRACT_XLSX_PATH

    return _county_name_from_fips_at(str(RUCA_TRACT_XLSX_PATH))


@lru_cache(maxsize=1)
def state_from_zip() -> dict[str, str]:
    """Return ZIP5 → state abbreviation from the RUCA ZIP-code workbook."""
    from ovid2.config.settings import RUCA_ZIPCODE_XLSX_PATH

    path = str(RUCA_ZIPCODE_XLSX_PATH)
    try:
        from openpyxl import load_workbook
    except ImportError:  # pragma: no cover - openpyxl is a runtime dep
        logger.warning("openpyxl unavailable; zip->state map empty.")
        return {}
    try:
        ws = load_workbook(path, data_only=True)["RUCA 2020 ZIP Code Data"]
    except (FileNotFoundError, KeyError, OSError) as exc:
        logger.warning("RUCA zipcode workbook unavailable (%s); zip->state map empty.", exc)
        return {}
    header = [cell.value for cell in ws[2]]
    zip_idx = header.index("ZIPCode")
    state_idx = header.index("State")
    result: dict[str, str] = {}
    for row in ws.iter_rows(min_row=3, values_only=True):
        zipcode, state = row[zip_idx], row[state_idx]
        if zipcode is not None and state is not None:
            result[str(zipcode).zfill(5)] = str(state)
    return result


@lru_cache(maxsize=1)
def county_from_zip() -> dict[str, str]:
    """Return ZIP5 → county name (ZIP5 → county FIPS → name)."""
    zip_to_fips = _county_fips_from_zip()
    fips_to_name = county_name_from_fips()
    return {z: fips_to_name[f] for z, f in zip_to_fips.items() if f in fips_to_name}


@lru_cache(maxsize=1)
def zcta_from_zip() -> dict[str, str]:
    """Return ZIP5 → most-common ZCTA from the geocoding CSV."""
    from ovid2.config.settings import GEOCODING_TABLE_PATH

    counts: dict[str, Counter] = {}
    try:
        with open(str(GEOCODING_TABLE_PATH), encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                zip9 = (row.get("ZIP9") or "").strip()
                zcta = (row.get("ZCTA") or "").strip()
                if not zip9 or not zcta:
                    continue
                counts.setdefault(zip9[:5], Counter())[zcta] += 1
    except (FileNotFoundError, OSError) as exc:
        logger.warning("Geocoding CSV unavailable (%s); zip->zcta map empty.", exc)
        return {}
    return {zip5: counter.most_common(1)[0][0] for zip5, counter in counts.items()}


# ---------------------------------------------------------------------------
# Internal file readers (not cached directly so tests can target a path)
# ---------------------------------------------------------------------------


def _county_name_from_fips_at(path: str) -> dict[str, str]:
    try:
        from openpyxl import load_workbook
    except ImportError:  # pragma: no cover
        logger.warning("openpyxl unavailable; fips->county map empty.")
        return {}
    try:
        ws = load_workbook(path, data_only=True)["RUCA2020 Tract Data"]
    except (FileNotFoundError, KeyError, OSError) as exc:
        logger.warning("RUCA tract workbook unavailable (%s); fips->county map empty.", exc)
        return {}
    header = [cell.value for cell in ws[2]]
    fips_idx = header.index("CountyFIPS20")
    name_idx = header.index("CountyName20")
    result: dict[str, str] = {}
    for row in ws.iter_rows(min_row=3, values_only=True):
        fips_val, name_val = row[fips_idx], row[name_idx]
        if fips_val is not None and name_val is not None:
            result[str(int(fips_val)).zfill(5)] = _strip_county_suffix(str(name_val))
    return result


@lru_cache(maxsize=1)
def _county_fips_from_zip() -> dict[str, str]:
    from ovid2.config.settings import GEOCODING_TABLE_PATH

    counts: dict[str, Counter] = {}
    try:
        with open(str(GEOCODING_TABLE_PATH), encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                zip9 = (row.get("ZIP9") or "").strip()
                state = (row.get("GEOCODE_STATE") or "").strip()
                county = (row.get("GEOCODE_COUNTY") or "").strip()
                if not zip9 or not state or not county:
                    continue
                counts.setdefault(zip9[:5], Counter())[state + county] += 1
    except (FileNotFoundError, OSError) as exc:
        logger.warning("Geocoding CSV unavailable (%s); zip->countyFIPS map empty.", exc)
        return {}
    return {zip5: counter.most_common(1)[0][0] for zip5, counter in counts.items()}


# ---------------------------------------------------------------------------
# Spark map-expression builders
# ---------------------------------------------------------------------------


def _map_expr(mapping: dict[str, str]) -> Column:
    """Return a Spark ``create_map`` Column from a Python dict.

    An empty mapping yields a map with one dummy entry so ``create_map``
    stays valid and every lookup returns ``NULL`` (the callers ``coalesce``
    over these, so a NULL simply falls through to the next source).
    """
    from pyspark.sql.functions import create_map, lit

    if not mapping:
        return create_map(lit("__ovid2_empty__"), lit(None))
    return create_map([lit(x) for x in chain.from_iterable(mapping.items())])


def fips_state_abbrev_map_expr() -> Column:
    """Return a ``create_map`` of state-FIPS → abbreviation."""
    return _map_expr(state_abbrev_from_fips())


def fips_county_name_map_expr() -> Column:
    """Return a ``create_map`` of county-FIPS → county name."""
    return _map_expr(county_name_from_fips())


def zip_state_map_expr() -> Column:
    """Return a ``create_map`` of ZIP5 → state abbreviation."""
    return _map_expr(state_from_zip())


def zip_county_map_expr() -> Column:
    """Return a ``create_map`` of ZIP5 → county name."""
    return _map_expr(county_from_zip())


def zip_zcta_map_expr() -> Column:
    """Return a ``create_map`` of ZIP5 → ZCTA."""
    return _map_expr(zcta_from_zip())


if __name__ == "__main__":
    raise SystemExit("This module provides geocoding lookups, not an executable script.")
