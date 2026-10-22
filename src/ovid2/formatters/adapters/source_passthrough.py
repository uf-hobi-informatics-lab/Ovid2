"""``source_passthrough`` adapter — CDM-agnostic source loader.

Reads one or more sources declared in the spec and returns their union
(by column name) as-is. Used by every pipeline whose input files already
match the target CDM's column shape — the ``projections:`` block just
renames / coalesces columns into their final CDM names.

This adapter contains **no CDM-specific logic**. Every non-OMOP pipeline
in Ovid2 uses it, regardless of whether the target CDM is PCORnet,
Nursing, Tumor, or Veritas.

.. note::

    Historically this adapter was named ``pcornet_passthrough`` because
    the first pipelines to use it produced PCORnet output. The alias is
    retained in :data:`ovid2.formatters.adapters.ADAPTERS` and in the
    JSON schema so existing specs keep loading. New specs should use
    ``source_passthrough``.
"""

from __future__ import annotations

from functools import reduce
from typing import TYPE_CHECKING

from ovid2.formatters.adapters._sources import read_source
from ovid2.formatters.adapters.base import AdapterContext, BaseAdapter
from ovid2.formatters.spec import FormatterSpec

if TYPE_CHECKING:
    from pyspark.sql import DataFrame


class SourcePassthroughAdapter(BaseAdapter):
    """Read one or more sources and return their name-wise union.

    Every source in the spec becomes a named DataFrame; the adapter
    unions them by column name so a spec with multiple sources produces
    a single wide DataFrame. When there is only one source, no union is
    performed.
    """

    def resolve(self, spec: FormatterSpec, ctx: AdapterContext) -> DataFrame:
        """Read the spec's sources and return their union.

        Raises:
            ValueError: When the spec declares no sources.
        """
        if not spec.sources:
            raise ValueError(
                f"source_passthrough requires at least one source; "
                f"spec '{spec.formatter}' has none."
            )

        dataframes = [
            read_source(
                source,
                spark=ctx.spark,
                input_address=ctx.input_address,
                pipeline=ctx.pipeline,
                batch_id=ctx.batch_id,
            )
            for source in spec.sources.values()
        ]

        # Fast-path: single source → nothing to union.
        if len(dataframes) == 1:
            return dataframes[0]

        # Union by name so pipelines can ship files with slightly different
        # column orderings across batches.
        return reduce(lambda a, b: a.unionByName(b, allowMissingColumns=True), dataframes)
