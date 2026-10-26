"""Sampling transforms: random_sample / limit / stratified_sample /
head_by_group.

All lazy: ``sample``/``limit``/``sampleBy`` are portable DataFrame
methods that add a plan node rather than materialising. ``head_by_group``
composes a window (Spark-family gated) with a filter.
"""

from __future__ import annotations

from typing import Any

from ovid2.transforms.base import BaseTransform, DataFrame, TransformContext


class RandomSampleTransform(BaseTransform):
    """Take a random fraction of rows (Bernoulli sampling)."""

    category = "sampling"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "random_sample"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Take a random fraction of rows (optionally seeded, reproducible)."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return {
            "type": "object",
            "required": ["fraction"],
            "additionalProperties": False,
            "properties": {
                "fraction": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 1,
                    "description": "Fraction of rows to keep, in (0, 1].",
                },
                "seed": {"type": "integer", "description": "Optional RNG seed."},
                "with_replacement": {"type": "boolean", "default": False},
            },
        }

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return a lazily-sampled DataFrame."""
        fraction = float(params["fraction"])
        if not 0 < fraction <= 1:
            raise ValueError("random_sample: 'fraction' must be in (0, 1].")
        with_replacement = bool(params.get("with_replacement", False))
        seed = params.get("seed")
        if seed is None:
            return df.sample(withReplacement=with_replacement, fraction=fraction)
        return df.sample(withReplacement=with_replacement, fraction=fraction, seed=int(seed))


class LimitTransform(BaseTransform):
    """Keep at most ``n`` rows (lazy)."""

    category = "sampling"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "limit"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Keep at most N rows."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return {
            "type": "object",
            "required": ["n"],
            "additionalProperties": False,
            "properties": {"n": {"type": "integer", "minimum": 1}},
        }

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return the first ``n`` rows (lazy)."""
        n = int(params["n"])
        if n < 1:
            raise ValueError("limit: 'n' must be >= 1.")
        return df.limit(n)


class StratifiedSampleTransform(BaseTransform):
    """Sample by fraction-per-key using ``sampleBy``."""

    category = "sampling"
    requires_family = "spark"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "stratified_sample"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Sample a per-key fraction of rows, preserving key distribution."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return {
            "type": "object",
            "required": ["key", "fractions"],
            "additionalProperties": False,
            "properties": {
                "key": {"type": "string", "description": "Column to stratify on."},
                "fractions": {
                    "type": "object",
                    "minProperties": 1,
                    "additionalProperties": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "description": "{key_value: fraction} map.",
                },
                "seed": {"type": "integer"},
            },
        }

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return the stratified sample (lazy)."""
        key = str(params["key"])
        fractions_raw = params.get("fractions")
        if not isinstance(fractions_raw, dict) or not fractions_raw:
            raise ValueError("stratified_sample: 'fractions' must be a non-empty object.")
        fractions = {k: float(v) for k, v in fractions_raw.items()}
        seed = params.get("seed")
        stat = df.stat
        if seed is None:
            return stat.sampleBy(key, fractions)
        return stat.sampleBy(key, fractions, int(seed))


class HeadByGroupTransform(BaseTransform):
    """Keep the top-N rows per group by an ordering.

    Composes a ``row_number()`` window with a ``<= n`` filter — the
    canonical "latest record per patient" pattern. Spark-family gated
    because it uses a window.
    """

    category = "sampling"
    requires_family = "spark"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "head_by_group"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Keep the top-N rows per group by an ordering (e.g. latest per key)."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return {
            "type": "object",
            "required": ["partition_by", "order_by", "n"],
            "additionalProperties": False,
            "properties": {
                "partition_by": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                },
                "order_by": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["col"],
                        "properties": {
                            "col": {"type": "string"},
                            "desc": {"type": "boolean", "default": False},
                        },
                    },
                },
                "n": {"type": "integer", "minimum": 1},
            },
        }

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return the top-N-per-group DataFrame (lazy)."""
        from ovid2.transforms.ops._window import build_window, row_number_expr

        n = int(params["n"])
        if n < 1:
            raise ValueError("head_by_group: 'n' must be >= 1.")
        f = ctx.functions
        window = build_window(params, ctx)
        rn_col = "__ovid2_head_rn__"
        ranked = df.withColumn(rn_col, row_number_expr(ctx).over(window))
        return ranked.where(f.col(rn_col) <= f.lit(n)).drop(rn_col)


__all__ = [
    "HeadByGroupTransform",
    "LimitTransform",
    "RandomSampleTransform",
    "StratifiedSampleTransform",
]
