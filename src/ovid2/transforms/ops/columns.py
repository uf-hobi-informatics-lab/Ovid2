"""Column-shaping transforms: rename / select / drop / cast / pad /
coalesce_columns / default_value.

These operate on the DataFrame shape (add/remove/rename columns) using
the portable DataFrame API and the ``EngineFunctions`` DSL, never a
concrete engine module.
"""

from __future__ import annotations

from typing import Any

from ovid2.transforms.base import BaseTransform, DataFrame, TransformContext


class RenameColumnsTransform(BaseTransform):
    """Rename columns via an ``{old: new}`` map."""

    category = "formatting"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "rename_columns"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Rename columns using an {old: new} mapping."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return {
            "type": "object",
            "required": ["map"],
            "additionalProperties": False,
            "properties": {
                "map": {
                    "type": "object",
                    "minProperties": 1,
                    "additionalProperties": {"type": "string"},
                    "description": "{old_name: new_name} rename map.",
                }
            },
        }

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return ``df`` with columns renamed."""
        mapping = params.get("map")
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError("rename_columns: 'map' must be a non-empty object.")
        for old, new in mapping.items():
            df = df.withColumnRenamed(str(old), str(new))
        return df


class SelectColumnsTransform(BaseTransform):
    """Project down to an explicit, ordered list of columns."""

    category = "reshape"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "select_columns"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Keep only the listed columns, in the given order."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return {
            "type": "object",
            "required": ["columns"],
            "additionalProperties": False,
            "properties": {
                "columns": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                }
            },
        }

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return ``df`` projected to the named columns."""
        columns = params.get("columns")
        if not isinstance(columns, list) or not columns:
            raise ValueError("select_columns: 'columns' must be a non-empty list.")
        f = ctx.functions
        return df.select(*[f.col(str(c)) for c in columns])


class DropColumnsTransform(BaseTransform):
    """Drop a list of columns."""

    category = "reshape"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "drop_columns"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Remove the listed columns."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return {
            "type": "object",
            "required": ["columns"],
            "additionalProperties": False,
            "properties": {
                "columns": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                }
            },
        }

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return ``df`` with the named columns dropped."""
        columns = params.get("columns")
        if not isinstance(columns, list) or not columns:
            raise ValueError("drop_columns: 'columns' must be a non-empty list.")
        return df.drop(*[str(c) for c in columns])


class CastTransform(BaseTransform):
    """Cast columns to target types via a ``{column: type}`` map."""

    category = "formatting"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "cast"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Cast columns to target SQL types using a {column: type} map."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return {
            "type": "object",
            "required": ["types"],
            "additionalProperties": False,
            "properties": {
                "types": {
                    "type": "object",
                    "minProperties": 1,
                    "additionalProperties": {"type": "string"},
                    "description": "{column: sql_type}, e.g. {AGE: int, DOB: date}.",
                }
            },
        }

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return ``df`` with the named columns cast."""
        types = params.get("types")
        if not isinstance(types, dict) or not types:
            raise ValueError("cast: 'types' must be a non-empty object.")
        f = ctx.functions
        for column, sql_type in types.items():
            name = str(column)
            df = df.withColumn(name, f.col(name).cast(str(sql_type)))
        return df


class PadTransform(BaseTransform):
    """Pad a column to a fixed width with a fill character.

    Left-pad is the default (zero-padded ids). Set ``side: right`` for
    right-padding.
    """

    category = "formatting"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "pad"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Pad a column to a fixed width with a fill character (left or right)."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return {
            "type": "object",
            "required": ["column", "width"],
            "additionalProperties": False,
            "properties": {
                "column": {"type": "string"},
                "width": {"type": "integer", "minimum": 1},
                "char": {"type": "string", "default": "0", "maxLength": 1},
                "side": {"type": "string", "enum": ["left", "right"], "default": "left"},
            },
        }

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return ``df`` with the column padded to width."""
        column = str(params["column"])
        width = int(params["width"])
        char = str(params.get("char", "0"))
        if len(char) != 1:
            raise ValueError("pad: 'char' must be a single character.")
        side = str(params.get("side", "left")).lower()
        if side not in ("left", "right"):
            raise ValueError("pad: 'side' must be 'left' or 'right'.")
        # lpad/rpad are standard SQL string functions available on both
        # families; reach them through the portable expr entry so we do
        # not have to widen EngineFunctions for this single use.
        fn = "lpad" if side == "left" else "rpad"
        f = ctx.functions
        padded = f.expr(f"{fn}(cast(`{column}` as string), {width}, '{char}')")
        return df.withColumn(column, padded)


class CoalesceColumnsTransform(BaseTransform):
    """First-non-null across a list of columns into a target column."""

    category = "correction"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "coalesce_columns"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Write the first non-null of several columns into a target column."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return {
            "type": "object",
            "required": ["target", "columns"],
            "additionalProperties": False,
            "properties": {
                "target": {"type": "string"},
                "columns": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                },
            },
        }

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return ``df`` with ``target`` set to the first non-null source."""
        target = str(params["target"])
        columns = params.get("columns")
        if not isinstance(columns, list) or not columns:
            raise ValueError("coalesce_columns: 'columns' must be a non-empty list.")
        f = ctx.functions
        return df.withColumn(target, f.coalesce(*[f.col(str(c)) for c in columns]))


class DefaultValueTransform(BaseTransform):
    """Fill nulls in a column with a literal default."""

    category = "correction"

    @property
    def name(self) -> str:
        """Return the transform identifier."""
        return "default_value"

    @property
    def summary(self) -> str:
        """Return a one-line description."""
        return "Replace nulls in a column with a constant default value."

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return the JSON-schema fragment for this op."""
        return {
            "type": "object",
            "required": ["column", "value"],
            "additionalProperties": False,
            "properties": {
                "column": {"type": "string"},
                "value": {
                    "description": "Literal default (string / number / boolean).",
                },
            },
        }

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: TransformContext) -> DataFrame:
        """Return ``df`` with nulls in the column replaced by the default."""
        column = str(params["column"])
        value = params["value"]
        f = ctx.functions
        return df.withColumn(column, f.coalesce(f.col(column), f.lit(value)))


__all__ = [
    "CastTransform",
    "CoalesceColumnsTransform",
    "DefaultValueTransform",
    "DropColumnsTransform",
    "PadTransform",
    "RenameColumnsTransform",
    "SelectColumnsTransform",
]
