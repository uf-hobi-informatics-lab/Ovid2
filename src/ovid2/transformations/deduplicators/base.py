"""Skew-relief deduplicator with null-key passthrough and salted two-stage dedup.

The naive :meth:`pyspark.sql.DataFrame.dropDuplicates` hash-partitions rows
across ``spark.sql.shuffle.partitions`` buckets. On skewed inputs (e.g.
after LEFT OUTER joins that create a hot partition, or null/sentinel
values collapsing into one bucket) a single task ends up with a
catastrophic share of the data while every other executor idles.

This module applies two independent mitigations:

1. **Null-key passthrough** — rows whose composite key has any null or
   empty-string component skip dedup entirely (they cannot be mutual
   duplicates anyway).
2. **Salted two-stage dedup** — inserts a random salt column into the key
   for stage 1, then a final dedup on the real key. Splits any hot hash
   bucket across N partitions in stage 1, restoring parallelism.

Both mitigations preserve output semantics.

Composite keys are resolved through the CDM handle passed on the pipeline
context — this module contains no CDM-specific imports.
"""

from __future__ import annotations

import operator
from functools import reduce
from typing import TYPE_CHECKING, Any

from pyspark.sql import functions as sf

from ovid2.config.settings import DEDUP_NULL_KEY_PASSTHROUGH, DEDUP_SALT_BUCKETS

if TYPE_CHECKING:
    from pyspark.sql import DataFrame

    from ovid2.cdm.handle import CdmHandle
    from ovid2.core.engines.base import BaseEngine

_SALT_COL: str = "__dedup_salt"


class Deduplicator:
    """Composite-key deduplicator with skew relief.

    Attributes:
        table_name: The CDM table being deduplicated.
        key_fields: Composite-key columns resolved from the CDM handle.
        engine: Optional compute engine. When it is not Spark-compatible
            (Snowpark), salting — a Spark-only skew mitigation — is
            skipped in favour of a single-stage
            :meth:`BaseEngine.drop_duplicates`, and the null-key
            predicate / union use the engine's portable DSL. When the
            engine is ``None`` or Spark-compatible, the byte-identical
            pre-portability PySpark path runs.
    """

    def __init__(
        self,
        table_name: str,
        *,
        cdm: "CdmHandle | None" = None,
        key_fields: list[str] | None = None,
        engine: "BaseEngine | None" = None,
    ) -> None:
        """Initialise the deduplicator.

        Args:
            table_name: The CDM table being deduplicated.
            cdm: The CDM handle to consult for composite keys. When
                omitted and ``key_fields`` is also omitted, defaults to
                the pcornet_7_0 registration for backwards compatibility.
            key_fields: Explicit override for the composite key. When
                supplied, ``cdm`` is not consulted.
            engine: Optional compute engine. Drives the engine-portable
                (Snowpark) dedup path when its session is not Spark; when
                ``None`` or Spark-compatible the classic PySpark path runs.
        """
        self.table_name: str = table_name
        self.engine: "BaseEngine | None" = engine
        if key_fields is not None:
            self.key_fields: list[str] = list(key_fields)
            return

        if cdm is None:
            # Default to the pcornet handle so callers that don't yet plumb
            # the CDM through the context keep working. Once every call
            # site passes ``cdm=``, this fallback can be removed.
            from ovid2.cdm.registry import get_cdm

            cdm = get_cdm("pcornet_7_0")

        try:
            self.key_fields = list(cdm.dedup_key_for(table_name))
        except ValueError as exc:
            raise ValueError(
                f"No composite key for table '{table_name}' in CDM '{cdm.name}'."
            ) from exc

    # ------------------------------------------------------------------
    # Engine capability
    # ------------------------------------------------------------------

    @property
    def _is_spark(self) -> bool:
        """True when the classic PySpark path should run.

        No engine attached (unit tests, legacy call sites) is treated as
        Spark for backwards compatibility.
        """
        return self.engine is None or getattr(self.engine, "session_is_spark", True)

    def deduplicate(self, df: DataFrame) -> DataFrame:
        """Return ``df`` with duplicates removed per the configured strategy.

        Args:
            df: Input DataFrame.

        Returns:
            Deduplicated DataFrame (lazy).
        """
        if self._is_spark:
            return self._deduplicate_spark(df)
        return self._deduplicate_portable(df)

    # ------------------------------------------------------------------
    # PySpark path (byte-identical to the pre-portability implementation)
    # ------------------------------------------------------------------

    def _deduplicate_spark(self, df: DataFrame) -> DataFrame:
        if not self.key_fields:
            return df.dropDuplicates()

        if DEDUP_NULL_KEY_PASSTHROUGH:
            full_key_predicate = self._build_full_key_predicate()
            with_full_key = df.filter(full_key_predicate)
            without_full_key: DataFrame | None = df.filter(~full_key_predicate)
        else:
            with_full_key = df
            without_full_key = None

        deduped = self._dedup_with_skew_relief(with_full_key)
        if without_full_key is None:
            return deduped
        return deduped.unionByName(without_full_key)

    def _build_full_key_predicate(self) -> sf.Column:
        """Build a predicate that is true iff every key column is populated."""
        predicates = [sf.col(k).isNotNull() & (sf.col(k) != sf.lit("")) for k in self.key_fields]
        return reduce(operator.and_, predicates)

    def _dedup_with_skew_relief(self, df: DataFrame) -> DataFrame:
        """Apply salted two-stage dedup, or single-stage when salting is disabled."""
        if DEDUP_SALT_BUCKETS < 2:
            return df.dropDuplicates(self.key_fields)

        # Guard against a user-provided column colliding with the salt name.
        salt_col = _SALT_COL
        while salt_col in df.columns:
            salt_col = f"{salt_col}_"

        n_buckets = int(DEDUP_SALT_BUCKETS)
        salted = df.withColumn(
            salt_col,
            (sf.rand(seed=42) * sf.lit(n_buckets)).cast("int"),
        )
        stage1 = salted.dropDuplicates(self.key_fields + [salt_col])
        return stage1.drop(salt_col).dropDuplicates(self.key_fields)

    # ------------------------------------------------------------------
    # Engine-portable path (Snowpark)
    # ------------------------------------------------------------------

    def _deduplicate_portable(self, df: Any) -> Any:
        """Single-stage dedup via the engine's portable helpers.

        Salting is a Spark-only skew mitigation — Snowflake plans its own
        shuffles, so a plain ``drop_duplicates`` on the composite key is
        both correct and idiomatic. Null-key passthrough is preserved for
        output-semantics parity with the Spark path (rows with a null /
        empty key component cannot be mutual duplicates).
        """
        engine = self.engine
        assert engine is not None  # _is_spark guards this
        if not self.key_fields:
            return engine.drop_duplicates(df)

        fns = engine.get_functions()
        if DEDUP_NULL_KEY_PASSTHROUGH:
            predicate = self._build_full_key_predicate_portable(fns)
            with_full_key = df.filter(predicate)
            without_full_key = df.filter(~predicate)
            deduped = engine.drop_duplicates(with_full_key, self.key_fields)
            return engine.union_by_name(deduped, without_full_key)
        return engine.drop_duplicates(df, self.key_fields)

    def _build_full_key_predicate_portable(self, fns: Any) -> Any:
        """Portable analogue of :meth:`_build_full_key_predicate`."""
        predicates = [
            fns.col(k).isNotNull() & (fns.col(k) != fns.lit("")) for k in self.key_fields
        ]
        return reduce(operator.and_, predicates)
