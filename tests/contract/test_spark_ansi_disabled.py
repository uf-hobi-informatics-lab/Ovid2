"""Contract: the PySpark engine keeps Spark 3 (non-ANSI) SQL semantics.

Spark 4 enables ``spark.sql.ansi.enabled`` by default. Under ANSI a
malformed cast, an unparseable ``to_date`` / ``to_timestamp`` or an
integer overflow raises instead of returning NULL. Ovid2's formatters,
fixers and health ops depend on NULL-on-failure (multi-format
``coalesce(to_date(...))`` chains, ``cast("double").isNotNull()``
numeric probes), so turning ANSI on would abort real ETL runs on
dirty source data. Flipping this needs a deliberate migration of
those call sites to ``try_cast`` / ``try_to_date``.
"""

from __future__ import annotations

import pytest

from ovid2.config.settings import SPARK_CONFIG


@pytest.mark.contract
def test_spark_config_disables_ansi_mode() -> None:
    assert SPARK_CONFIG.get("spark.sql.ansi.enabled") == "false"


@pytest.mark.contract
def test_spark_config_keeps_legacy_time_parser() -> None:
    assert SPARK_CONFIG.get("spark.sql.legacy.timeParserPolicy") == "LEGACY"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Contract test; run with pytest.")
