"""``semantic_join`` adapter — vocabulary-driven join, framework-level.

Generalises :class:`~ovid2.formatters.adapters.omop_concept_join.OmopConceptJoinAdapter`.
Instead of hardcoding the ``concept`` temp view, each join can reference
any :class:`~ovid2.semantic.handle.VocabularyHandle` registered in
:mod:`ovid2.semantic.registry`.

Spec shape (backwards-compatible with ``omop_concept_join``):

.. code-block:: yaml

    adapter: semantic_join
    sources:
      condition_occurrence:
        glob: 'CONDITION_OCCURRENCE.csv'
    joins:
      - with: omop_concept                # a registered vocabulary
        on: 'omop_concept.concept_id = condition_occurrence.condition_concept_id'
        prefix: cond
        domain_filter: ['Condition']
    projections:
      DX: cond_concept_code
      DX_TYPE: cond_vocabulary_id

When ``with:`` names a registered vocabulary, the adapter resolves it
via :func:`ovid2.semantic.get_vocabulary` and joins against the
vocabulary's Spark temp view. When ``with:`` names any other symbol,
the adapter falls back to ``spark.table(name)`` — this is what makes
the adapter a strict superset of ``omop_concept_join`` (the ``concept``
temp view can still be referenced by that name).

Ambiguity guardrails carried over from ``omop_concept_join``:

- Every join must specify a ``prefix:`` and the adapter aliases every
  column of the right side by that prefix. After the join it is
  impossible to reference the vocabulary's ``concept_id`` (etc.)
  without picking a specific side.
- Sources are always lowercased at read time.
- ``domain_filter`` pre-filters the vocabulary before the join, using
  the vocabulary handle's ``domain_column``.

Lazy Spark guarantee:
    No ``.collect()`` / ``.count()`` / ``.take()``. Passes the
    ``test_no_collect_in_src.py`` contract.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ovid2.formatters.adapters._sources import read_source
from ovid2.formatters.adapters.base import AdapterContext, BaseAdapter
from ovid2.formatters.spec import FormatterSpec, JoinSpec

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession

logger = logging.getLogger(__name__)


class SemanticJoinAdapter(BaseAdapter):
    """Adapter that joins sources against one or more registered vocabularies."""

    def resolve(self, spec: FormatterSpec, ctx: AdapterContext) -> DataFrame:
        """Read every source, then apply each join. Returns a lazy DataFrame."""
        if not spec.sources:
            raise ValueError(
                f"semantic_join requires at least one source; spec '{spec.formatter}' has none."
            )

        source_views: dict[str, DataFrame] = {}
        for source in spec.sources.values():
            df = read_source(
                source,
                spark=ctx.spark,
                input_address=ctx.input_address,
                pipeline=ctx.pipeline,
                batch_id=ctx.batch_id,
            )
            source_views[source.name] = df
            df.createOrReplaceTempView(source.name)

        first_name = next(iter(source_views))
        df = source_views[first_name].alias(first_name)

        for join in spec.joins:
            right_df = self._prepare_right_side(join, ctx.spark)
            df = df.join(right_df, on=self._on_expr(join, ctx.spark), how=join.how)

        return df

    # ---------------------------------------------------------------- joins

    def _prepare_right_side(self, join: JoinSpec, spark: SparkSession) -> DataFrame:
        """Return the right side of ``join`` with every column prefixed.

        Resolution order for ``join.with_``:

        1. A registered semantic vocabulary — use its Spark temp view
           and its ``domain_column`` for the optional ``domain_filter``.
        2. Any other symbol — treated as a Spark temp view name.
        """
        from ovid2 import semantic as semantic_pkg

        try:
            vocab = semantic_pkg.get_vocabulary(join.with_)
        except ValueError:
            # Not a registered vocabulary — fall through to spark.table lookup.
            vocab = None

        if vocab is not None:
            base = spark.table(vocab.resolved_view_name())
            if join.domain_filter:
                if vocab.domain_column is None:
                    logger.warning(
                        "semantic_join: domain_filter set on vocabulary '%s' which has no "
                        "domain_column; skipping filter.",
                        vocab.name,
                    )
                else:
                    base = base.filter(base[vocab.domain_column].isin(list(join.domain_filter)))
        else:
            base = spark.table(join.with_)
            # Legacy ``concept`` view — apply domain_filter on ``domain_id``.
            if join.with_ == "concept" and join.domain_filter:
                base = base.filter(base.domain_id.isin(list(join.domain_filter)))

        # Rename every column to ``<prefix>_<col>`` *and* expose the frame
        # under the alias ``<prefix>``. The rename keeps projection
        # references terse (``cond_concept_code``); the alias lets the join
        # predicate qualify the key as ``<prefix>.<prefix>_<col>`` so it can
        # never be ambiguous with a base-source column of the same original
        # name (e.g. ``person.gender_concept_id`` alongside the concept's own
        # ``concept_id`` renamed to ``gender_concept_id``).
        renamed = base.toDF(*[f"{join.prefix}_{column}" for column in base.columns])
        return renamed.alias(join.prefix)

    def _on_expr(self, join: JoinSpec, spark: SparkSession) -> object:
        """Rewrite the join's ``on:`` predicate so column refs match the prefix.

        Users write joins naturally::

            on: "omop_concept.concept_id = condition_occurrence.condition_concept_id"

        After :meth:`_prepare_right_side` renames every right-side column
        (``omop_concept.concept_id`` → ``cond_concept_id``) and aliases the
        frame as ``cond``, we substitute each ``<with>.<col>`` occurrence
        into the fully-qualified ``<prefix>.<prefix>_<col>`` so the key is
        unambiguous even when a base source carries an identically-named
        column.
        """
        rewritten = join.on.replace(f"{join.with_}.", f"{join.prefix}.{join.prefix}_")
        from pyspark.sql.functions import expr

        return expr(rewritten)
