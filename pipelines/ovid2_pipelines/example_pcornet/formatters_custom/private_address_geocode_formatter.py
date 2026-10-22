"""example_pcornet private_address_geocode formatter — joins with the geocoding CSV.

Reads ``LDS_Address_History`` for ZIP data, joins with the shared
geocoding reference table on ZIP9 and ZIP5, and produces PCORnet
``private_address_geocode`` records with FIPS codes, RUCA, ZCTA, lat/lon,
and ZIP5 fallback logic. All operations are lazy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ovid2.formatters.base import BaseFormatter

if TYPE_CHECKING:
    from pyspark.sql import Column, DataFrame


INPUT_FILE_NAME: str = "*LDS_Address_History_*.txt"


def _geo_accuracy_col(ruca_2020_col: "Column", ruca_zip5_col: "Column") -> "Column":
    """Determine geocoding accuracy: ``Z9`` / ``Z5`` / ``NI``."""
    from pyspark.sql.functions import lit, when

    return (
        when(ruca_2020_col.isNull() & ruca_zip5_col.isNull(), lit("NI"))
        .when(ruca_2020_col.isNotNull(), lit("Z9"))
        .otherwise(lit("Z5"))
    )


def _shapefile_col(ruca_2020_col: "Column", ruca_zip5_col: "Column") -> "Column":
    """Return the shapefile reference matching the geocoding source used."""
    from pyspark.sql.functions import lit, when

    return (
        when(ruca_2020_col.isNull() & ruca_zip5_col.isNull(), lit("NI"))
        .when(ruca_2020_col.isNotNull(), lit("RUCA2020_ZIP9"))
        .otherwise(lit("RUCA2020_ZIP5"))
    )


def _zcta_col(zcta_col: "Column", zcta_zip5_col: "Column") -> "Column":
    """Coalesce the ZCTA from ZIP9, ZIP5, then ``NI``."""
    from pyspark.sql.functions import lit, when

    return (
        when(zcta_col.isNull() & zcta_zip5_col.isNull(), lit("NI"))
        .when(zcta_col.isNotNull(), zcta_col)
        .otherwise(zcta_zip5_col)
    )


class Private_address_geocodeFormatter(BaseFormatter):
    """Formatter for the example_pcornet ``private_address_geocode`` table."""

    @property
    def table_name(self) -> str:
        """Return the CDM table name."""
        return "private_address_geocode"

    @property
    def input_file_name(self) -> str:
        """Return the input file glob."""
        return INPUT_FILE_NAME

    def format(self, df: "DataFrame") -> "DataFrame":  # noqa: A003 — CDM API
        """Enrich example_pcornet address data with geocoding lookups (lazy)."""
        from pyspark.sql.functions import (
            coalesce,
            col,
            concat,
            current_timestamp,
            date_format,
            lit,
            monotonically_increasing_id,
            trim,
        )

        from ovid2.config.settings import GEOCODING_TABLE_PATH
        from ovid2.utils.common import validate_zip5_col, validate_zip9_col
        from ovid2.utils.geocoding import (
            fips_county_name_map_expr,
            fips_state_abbrev_map_expr,
            zip_county_map_expr,
            zip_state_map_expr,
        )

        spark = df.sparkSession

        address = df.select(
            col("ADDRESSID").alias("ADDRESSID"),
            validate_zip5_col(col("ADDRESS_ZIP5")).alias("ADDRESS_ZIP5"),
            validate_zip9_col(col("ADDRESS_ZIP9")).alias("ADDRESS_ZIP9"),
        )

        geocoding = spark.read.load(
            str(GEOCODING_TABLE_PATH),
            format="csv",
            sep=",",
            inferSchema="false",
            header="true",
            quote='"',
        ).withColumn(
            "FULL_GEOCODE_COUNTY",
            concat(col("GEOCODE_STATE"), col("GEOCODE_COUNTY")),
        )

        geocoding_zip5 = (
            geocoding.withColumn("ZIP9", col("ZIP9").cast("string"))
            .filter(col("ZIP9").isNull() | (trim(col("ZIP9")) == ""))
            .select(
                col("ZIP5").cast("string").alias("ZIP5"),
                col("RUCA_2020").alias("RUCA_ZIP5"),
                col("GEOCODE_STATE").alias("STATE_ZIP5"),
                col("FULL_GEOCODE_COUNTY").alias("COUNTY_ZIP5"),
                col("ZCTA").alias("ZCTA_ZIP5"),
            )
        )

        geo_joined = address.join(
            geocoding, geocoding["ZIP9"] == address["ADDRESS_ZIP9"], how="left"
        ).join(geocoding_zip5, geocoding_zip5["ZIP5"] == address["ADDRESS_ZIP5"], how="left")

        return geo_joined.select(
            concat(
                date_format(current_timestamp(), "yyyyMMddHHmmssSSS"),
                monotonically_increasing_id(),
            ).alias("GEOCODEID"),
            col("ADDRESSID").alias("ADDRESSID"),
            coalesce(
                fips_state_abbrev_map_expr()[col("GEOCODE_STATE")],
                zip_state_map_expr()[col("ADDRESS_ZIP5")],
                lit(""),
            ).alias("GEOCODE_STATE"),
            coalesce(
                fips_county_name_map_expr()[col("FULL_GEOCODE_COUNTY")],
                zip_county_map_expr()[col("ADDRESS_ZIP5")],
                lit(""),
            ).alias("GEOCODE_COUNTY"),
            coalesce(col("X"), lit("")).alias("GEOCODE_LONGITUDE"),
            coalesce(col("Y"), lit("")).alias("GEOCODE_LATITUDE"),
            coalesce(col("GEOCODE_BLOCK_15dig"), lit("")).alias("GEOCODE_BLOCK"),
            coalesce(col("GEOCODE_TRACT_11dig"), lit("")).alias("GEOCODE_TRACT"),
            coalesce(col("GEOCODE_GROUP"), lit("")).alias("GEOCODE_GROUP"),
            _zcta_col(col("ZCTA"), col("ZCTA_ZIP5")).alias("GEOCODE_ZCTA"),
            lit("").alias("GEOCODE_CUSTOM"),
            lit("").alias("GEOCODE_CUSTOM_TEXT"),
            _shapefile_col(col("RUCA_2020"), col("RUCA_ZIP5")).alias("SHAPEFILE"),
            _geo_accuracy_col(col("RUCA_2020"), col("RUCA_ZIP5")).alias("GEO_ACCURACY"),
            lit("FULL_GEOCODING_202603/RUCA-2020").alias("GEO_PROV_REF"),
            date_format(current_timestamp(), "yyyyMMdd").alias("ASSIGNMENT_DATE"),
        )
