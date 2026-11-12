"""Transform stage — runs the pipeline's assigned transform chains.

The fifth canonical ETL stage, between ``deduplicate`` and ``map``. For
``context['table_name']`` it looks up the chains assigned in
``pipelines/<pipeline>/transforms/assignments.yaml``
(:mod:`ovid2.transforms.assignments`) and runs each through the
transforms engine against the *active* compute engine (Spark, DuckDB or
Snowpark). A pipeline with no assignments makes this stage a no-op, so
it is always safe to include in ``--transformations all``.

Everything stays lazy: transform ops are column expressions and
DataFrame-API calls only.
"""

from __future__ import annotations

import logging
from typing import Any

from ovid2.transformations.base import BaseTransformer

#: Engine-neutral frame (Spark / DuckFrame / Snowpark DataFrame).
DataFrame = Any

logger = logging.getLogger(__name__)

__all__ = ["TransformStageTransformer"]


class TransformStageTransformer(BaseTransformer):
    """Apply the pipeline's per-table transform assignments."""

    def transform(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Run every chain assigned to ``context['table_name']`` in order."""
        from ovid2.transforms import engine as transforms_engine
        from ovid2.transforms import spec as transforms_spec
        from ovid2.transforms.assignments import load_assignments, table_assignments
        from ovid2.transforms.runtime import engine_context_for

        table = context.get("table_name")
        pipeline = context.get("pipeline")
        if not table or not pipeline:
            return df
        chains = table_assignments(load_assignments(str(pipeline)), str(table))
        if not chains:
            return df

        functions, family = engine_context_for(df, context.get("engine"))
        session: Any = context.get("spark")
        for index, assignment in enumerate(chains, 1):
            if assignment.spec:
                chain = transforms_spec.load_spec(assignment.spec, pipeline=str(pipeline))
            else:
                chain = transforms_spec.load_from_dict(
                    {"v": 1, "steps": list(assignment.steps or ())},
                    name=f"{pipeline}.{table}.inline{index}",
                    origin=f"{pipeline}/transforms/assignments.yaml[{table}][{index}]",
                )
            logger.info(
                "transform stage: %s/%s ← %s (%d steps)",
                pipeline,
                table,
                assignment.label,
                len(chain.steps),
            )
            df = transforms_engine.run(
                df,
                chain,
                session=session,
                functions=functions,
                engine_family=family,
                pipeline=str(pipeline),
            )
        return df


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.transformations.transform is a library module.")
