"""``translate_by_dict`` — substitute source values via a pipeline dictionary.

Params:
    column: Column to translate.
    dict: Name of the ``dict[str, str]`` in the pipeline's
        ``dictionaries`` module. Convention: ``<table>_<column>_dict``.
    default: Optional literal value to fall back on when the source
        value is not in the dict. When omitted, unmatched values pass
        through unchanged (matches legacy behaviour).
    keep_null: When ``True`` (default), null input stays null. When
        ``False``, null maps to ``default`` if provided.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ovid2.mappers.base import BaseMapOp, MapOpContext

if TYPE_CHECKING:
    from pyspark.sql import DataFrame

logger = logging.getLogger(__name__)


class TranslateByDictOp(BaseMapOp):
    """Apply a value dictionary to one column."""

    @property
    def name(self) -> str:
        """Return the op identifier."""
        return "translate_by_dict"

    def apply(
        self,
        df: DataFrame,
        params: dict[str, Any],
        ctx: MapOpContext,
    ) -> DataFrame:
        """Substitute values in ``params['column']`` using the named dict."""
        from pyspark.sql.functions import coalesce, create_map, lit
        from pyspark.sql.functions import col as sf_col

        column = str(params["column"])
        dict_name = str(params["dict"])
        default = params.get("default")
        keep_null = bool(params.get("keep_null", True))

        if ctx.pipeline_dictionaries is None:
            logger.warning(
                "translate_by_dict: pipeline '%s' has no dictionaries module; skipping",
                ctx.pipeline,
            )
            return df

        translation = getattr(ctx.pipeline_dictionaries, dict_name, None)
        if not isinstance(translation, dict):
            logger.warning(
                "translate_by_dict: dict '%s' not found or wrong type in %s.dictionaries; skipping",
                dict_name,
                ctx.pipeline,
            )
            return df

        if not translation:
            return df

        # Build a Spark map literal from the dict for a lazy replace.
        pairs: list[Any] = []
        for src_val, dst_val in translation.items():
            pairs.append(lit(src_val))
            pairs.append(lit(dst_val))
        map_expr = create_map(*pairs)

        translated = map_expr[sf_col(column)]

        if default is not None:
            if keep_null:
                # Keep true nulls; only apply default for unmatched non-null values.
                result_expr = (
                    coalesce(translated, lit(default)).otherwise(lit(None))
                    if False
                    else translated  # placeholder; see combined expression below
                )
                # Combined expression: null in → null out; matched → mapped;
                # unmatched non-null → default.
                from pyspark.sql.functions import when

                result_expr = when(sf_col(column).isNull(), lit(None)).otherwise(
                    coalesce(translated, lit(default))
                )
            else:
                result_expr = coalesce(translated, lit(default))
        else:
            # Unmatched values pass through unchanged.
            result_expr = coalesce(translated, sf_col(column))

        return df.withColumn(column, result_expr)
