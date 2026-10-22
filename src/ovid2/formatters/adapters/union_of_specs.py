"""``union_of_specs`` adapter.

Composes several sub-specs and unions their outputs. Used by VITAL and
similar tables that draw rows from multiple heterogeneous sources
(e.g. OMOP ``measurement`` ∪ ``observation`` ∪ ``observation_sup``),
each of which is itself expressible as a ``pcornet_passthrough`` or
``omop_concept_join`` spec.

Each ``sub_specs`` entry names another spec file relative to the same
pipeline's ``formatters/`` directory. Sub-specs are executed end-to-end
(source read + joins + projections + post-expressions + column
validation) and their **projected** DataFrames are then unioned.
"""

from __future__ import annotations

from functools import reduce
from typing import TYPE_CHECKING

from ovid2.formatters.adapters.base import AdapterContext, BaseAdapter
from ovid2.formatters.spec import FormatterSpec, find_and_load

if TYPE_CHECKING:
    from pyspark.sql import DataFrame


class UnionOfSpecsAdapter(BaseAdapter):
    """Adapter that unions the outputs of several sub-specs."""

    def resolve(self, spec: FormatterSpec, ctx: AdapterContext) -> DataFrame:
        if not spec.sub_specs:
            raise ValueError(
                f"union_of_specs requires 'sub_specs'; spec '{spec.formatter}' has none."
            )

        # Import late to avoid a circular dep between engine and adapters.
        from ovid2.formatters.engine import execute_spec

        dataframes: list[DataFrame] = []
        for ref in spec.sub_specs:
            sub_spec = find_and_load(ctx.pipeline, ref.spec)
            dataframes.append(execute_spec(sub_spec, ctx))

        if not dataframes:
            raise ValueError(f"union_of_specs '{spec.formatter}' produced no DataFrames.")

        return reduce(
            lambda a, b: a.unionByName(b, allowMissingColumns=True),
            dataframes,
        )
