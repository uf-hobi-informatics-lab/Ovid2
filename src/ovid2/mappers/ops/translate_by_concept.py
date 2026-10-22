"""``translate_by_concept`` — substitute source values via a semantic vocabulary.

This op is the semantic-layer analogue of :mod:`translate_by_dict`. Where
``translate_by_dict`` reads a per-pipeline Python ``dict[str, str]``,
``translate_by_concept`` reads from a Spark temp view backed by a
:class:`~ovid2.semantic.handle.VocabularyHandle` registered in
:mod:`ovid2.semantic.registry`.

The Spark temp view is expected to be registered by
:func:`ovid2.core.pipeline.register_shared_tables` (for vocabularies
that ship as :class:`~ovid2.cdm.handle.SharedViewSpec`s on the CDM
handle — currently the case for ``omop_concept``). Vocabularies that do
not ride along a CDM's ``shared_views`` tuple will be registered on
demand in phase S3; this op emits a warning and no-ops when the view
does not exist.

Params:
    column: Column whose values should be translated. Also the default
        write-back column unless ``target_column`` overrides it.
    vocabulary: Name of the registered
        :class:`~ovid2.semantic.handle.VocabularyHandle` (e.g.
        ``'omop_concept'``, ``'loinc'``).
    match: Column *in the vocabulary view* to match against. Defaults
        to the vocabulary handle's ``code_column``.
    target: Column *in the vocabulary view* whose value replaces the
        source. Defaults to the vocabulary handle's ``code_column`` —
        effectively a filter-by-existence when target == match.
    target_column: Optional output column name (defaults to ``column``).
    keep_unmatched: When ``True`` (default), unmatched values pass
        through unchanged. When ``False``, unmatched rows are dropped.
    domain_filter: Optional list of values used to pre-filter the
        vocabulary view on its ``domain_column`` (e.g. ``['Condition']``
        for the OMOP CONCEPT domain). Ignored when the vocabulary has
        no ``domain_column``.
    case_sensitive: When ``False`` (default), match/target comparison
        is case-insensitive. Both sides are ``upper()``-normalised for
        the join.

Lazy Spark guarantee:
    Uses a broadcast left join. No ``.collect()`` / ``.count()`` /
    ``.take()``. Passes the ``test_no_collect_in_src.py`` contract.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ovid2.mappers.base import BaseMapOp, MapOpContext

if TYPE_CHECKING:
    from pyspark.sql import DataFrame

logger = logging.getLogger(__name__)


class TranslateByConceptOp(BaseMapOp):
    """Apply a semantic vocabulary translation to one column."""

    @property
    def name(self) -> str:
        """Return the op identifier."""
        return "translate_by_concept"

    def apply(
        self,
        df: DataFrame,
        params: dict[str, Any],
        ctx: MapOpContext,
    ) -> DataFrame:
        """Broadcast-join ``params['column']`` against the vocabulary view."""
        from pyspark.sql.functions import broadcast, col, upper

        try:
            from pyspark.errors import AnalysisException
        except ImportError:  # pyspark < 3.4
            from pyspark.sql.utils import (  # type: ignore[attr-defined]
                AnalysisException,
            )

        # Local import — go through the registry API only. Enforced by
        # the ``test_semantic_isolation.py`` contract.
        from ovid2 import semantic as semantic_pkg

        column = str(params["column"])
        vocabulary_name = str(params["vocabulary"])
        target_column = str(params.get("target_column") or column)
        keep_unmatched = bool(params.get("keep_unmatched", True))
        case_sensitive = bool(params.get("case_sensitive", False))
        domain_filter = params.get("domain_filter")

        try:
            vocab = semantic_pkg.get_vocabulary(vocabulary_name)
        except ValueError:
            logger.warning(
                "translate_by_concept: vocabulary '%s' is not registered; skipping op for %s.%s",
                vocabulary_name,
                ctx.pipeline,
                ctx.table,
            )
            return df

        match_col = str(params.get("match") or vocab.code_column)
        target_col = str(params.get("target") or vocab.code_column)

        view_name = vocab.resolved_view_name()

        try:
            vocab_df = ctx.spark.table(view_name)
        except AnalysisException:
            logger.warning(
                "translate_by_concept: vocabulary view '%s' is not registered in Spark; "
                "skipping op for %s.%s (vocabulary=%s). Ensure the CDM's shared_views "
                "or a semantic bootstrap has registered it.",
                view_name,
                ctx.pipeline,
                ctx.table,
                vocabulary_name,
            )
            return df

        # Optional domain pre-filter — applied *before* the join so the
        # broadcast side stays small even for large multi-domain vocabs.
        if domain_filter and vocab.domain_column:
            vocab_df = vocab_df.filter(col(vocab.domain_column).isin(list(domain_filter)))

        # Project only the columns we need — again to keep the broadcast small.
        needed_cols = {match_col, target_col}
        vocab_df = vocab_df.select(*[col(c) for c in needed_cols])

        # Alias for a clean join expression and to avoid accidental
        # column collisions with the input DataFrame.
        vocab_alias = f"__vocab_{vocabulary_name}"
        vocab_df = vocab_df.alias(vocab_alias)
        input_alias = "__input"
        df_aliased = df.alias(input_alias)

        if case_sensitive:
            join_condition = col(f"{input_alias}.{column}") == col(f"{vocab_alias}.{match_col}")
        else:
            join_condition = upper(col(f"{input_alias}.{column}")) == upper(
                col(f"{vocab_alias}.{match_col}")
            )

        joined = df_aliased.join(broadcast(vocab_df), on=join_condition, how="left")

        # Resolve the translation choice per-row: prefer the mapped value;
        # fall back to the source value (or drop when keep_unmatched=False).
        translated_expr = col(f"{vocab_alias}.{target_col}")

        if keep_unmatched:
            # Coalesce back to the original source value on miss.
            from pyspark.sql.functions import coalesce

            result_expr = coalesce(translated_expr, col(f"{input_alias}.{column}"))
        else:
            # Drop rows where the join did not match.
            joined = joined.filter(translated_expr.isNotNull())
            result_expr = translated_expr

        # Preserve the input columns exactly, replacing/adding target_column.
        input_columns = [f"{input_alias}.{c}" for c in df.columns if c != target_column]
        select_exprs = [col(c) for c in input_columns]
        select_exprs.append(result_expr.alias(target_column))

        return joined.select(*select_exprs)
