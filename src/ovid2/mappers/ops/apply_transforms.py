"""``apply_transforms`` — bridge to the public transformations library.

Runs a named or inline transform chain (from
:mod:`ovid2.transforms`) inside a mapper spec. This makes the whole
CDM-agnostic transformations library usable from every pipeline's
mapper chain today, without a new pipeline stage.

Params (one of ``spec`` / ``steps`` required):
    spec: Name of a transform spec to load
        (``resources/transforms/<name>.yaml`` or the pipeline's
        ``transforms/<name>.yaml``).
    steps: Inline list of ``{op: ..., ...params}`` steps (an anonymous
        chain declared right in the mapper spec).

Example (inline)::

    ops:
      - op: apply_transforms
        steps:
          - op: trim
            columns: [SEX]
          - op: to_upper
            columns: [SEX]

Example (named shared spec)::

    ops:
      - op: apply_transforms
        spec: clean_whitespace_and_nulls
"""

from __future__ import annotations

from typing import Any

from ovid2.mappers.base import BaseMapOp, MapOpContext
from ovid2.transforms.base import DataFrame


class ApplyTransformsOp(BaseMapOp):
    """Run a public-library transform chain inside a mapper spec."""

    @property
    def name(self) -> str:
        """Return the op identifier."""
        return "apply_transforms"

    def apply(
        self,
        df: "DataFrame",
        params: dict[str, Any],
        ctx: MapOpContext,
    ) -> "DataFrame":
        """Resolve the transform chain and run it lazily against ``df``."""
        from ovid2.core.engines.pyspark import spark_functions
        from ovid2.transforms import engine as transforms_engine
        from ovid2.transforms import spec as transforms_spec

        spec_name = params.get("spec")
        inline_steps = params.get("steps")

        if spec_name is not None and inline_steps is not None:
            raise ValueError(
                "apply_transforms: provide exactly one of 'spec' or 'steps', not both."
            )

        if spec_name is not None:
            transform_spec = transforms_spec.load_spec(str(spec_name), pipeline=ctx.pipeline)
        elif inline_steps is not None:
            transform_spec = transforms_spec.load_from_dict(
                {"v": 1, "steps": inline_steps},
                name=f"{ctx.pipeline}.{ctx.table}.inline",
                origin=f"mapper {ctx.pipeline}/{ctx.table}",
            )
        else:
            raise ValueError("apply_transforms: one of 'spec' or 'steps' is required.")

        # The mapper tier is Spark-family; obtain the portable functions
        # DSL from the Spark engine without spinning a new session.
        functions = spark_functions()
        return transforms_engine.run(
            df,
            transform_spec,
            session=ctx.spark,  # type: ignore[arg-type]  # SparkSession satisfies EngineSession structurally
            functions=functions,
            engine_family="spark",
            pipeline=ctx.pipeline,
            secrets=ctx.secrets,
        )


__all__ = ["ApplyTransformsOp"]
