"""Web-safe transforms catalogue (discovery mirror).

Publishes the *metadata* of every transform in
:data:`ovid2.transforms.registry.TRANSFORMS` so the web tier
(``GET /api/v1/transforms``) and the MCP server
(``list_transforms``) can enumerate the public transformations
library **without importing the pipeline-tier**
``ovid2.transforms`` package (which pulls in DataFrame-shaped
op code). This mirrors the split between
:mod:`ovid2.core.engines` and :mod:`ovid2.config.engines`.

The contract test
``tests/contract/test_transforms_catalogue_synchronised.py``
cross-checks this catalogue against the registry so the two can
never drift.
"""

from __future__ import annotations

from typing import Any

#: One entry per registered transform. Keep in registry order-agnostic
#: (sorted by name); the contract test asserts full equality with the
#: live registry metadata.
TRANSFORM_CATALOGUE: list[dict[str, Any]] = [
    {
        "name": "add_row_number",
        "summary": "Add a 1-based row_number within partitions, ordered by columns.",
        "category": "reshape",
        "requires_family": "spark",
        "params_schema": {
            "type": "object",
            "required": ["order_by", "output_column"],
            "additionalProperties": False,
            "properties": {
                "partition_by": {"type": "array", "items": {"type": "string"}},
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
                "output_column": {"type": "string"},
            },
        },
    },
    {
        "name": "cast",
        "summary": "Cast columns to target SQL types using a {column: type} map.",
        "category": "formatting",
        "requires_family": None,
        "params_schema": {
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
        },
    },
    {
        "name": "clamp",
        "summary": "Bound a numeric column to a [min, max] range.",
        "category": "correction",
        "requires_family": None,
        "params_schema": {
            "type": "object",
            "required": ["column"],
            "additionalProperties": False,
            "properties": {
                "column": {"type": "string"},
                "min": {"type": "number"},
                "max": {"type": "number"},
            },
        },
    },
    {
        "name": "coalesce_columns",
        "summary": "Write the first non-null of several columns into a target column.",
        "category": "correction",
        "requires_family": None,
        "params_schema": {
            "type": "object",
            "required": ["target", "columns"],
            "additionalProperties": False,
            "properties": {
                "target": {"type": "string"},
                "columns": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "dedup",
        "summary": "Drop duplicate rows (optionally keyed on a subset of columns).",
        "category": "correction",
        "requires_family": None,
        "params_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "subset": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Columns that define uniqueness (omit for full-row).",
                }
            },
        },
    },
    {
        "name": "default_value",
        "summary": "Replace nulls in a column with a constant default value.",
        "category": "correction",
        "requires_family": None,
        "params_schema": {
            "type": "object",
            "required": ["column", "value"],
            "additionalProperties": False,
            "properties": {
                "column": {"type": "string"},
                "value": {"description": "Literal default (string / number / boolean)."},
            },
        },
    },
    {
        "name": "dense_rank",
        "summary": "Add a dense rank (no gaps for ties) within partitions.",
        "category": "reshape",
        "requires_family": "spark",
        "params_schema": {
            "type": "object",
            "required": ["order_by", "output_column"],
            "additionalProperties": False,
            "properties": {
                "partition_by": {"type": "array", "items": {"type": "string"}},
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
                "output_column": {"type": "string"},
            },
        },
    },
    {
        "name": "distinct",
        "summary": "Keep only distinct rows (full-row uniqueness).",
        "category": "correction",
        "requires_family": None,
        "params_schema": {"type": "object"},
    },
    {
        "name": "drop_columns",
        "summary": "Remove the listed columns.",
        "category": "reshape",
        "requires_family": None,
        "params_schema": {
            "type": "object",
            "required": ["columns"],
            "additionalProperties": False,
            "properties": {
                "columns": {"type": "array", "minItems": 1, "items": {"type": "string"}}
            },
        },
    },
    {
        "name": "explode",
        "summary": "Expand an array or map column into one row per element.",
        "category": "reshape",
        "requires_family": None,
        "params_schema": {
            "type": "object",
            "required": ["column"],
            "additionalProperties": False,
            "properties": {
                "column": {"type": "string"},
                "alias": {
                    "type": "string",
                    "description": "Name for the exploded value (defaults to the column name).",
                },
            },
        },
    },
    {
        "name": "filter_expr",
        "summary": "Keep only rows matching a boolean column expression.",
        "category": "correction",
        "requires_family": None,
        "params_schema": {
            "type": "object",
            "required": ["expr"],
            "additionalProperties": False,
            "properties": {
                "expr": {
                    "type": "string",
                    "description": "Boolean column predicate, e.g. 'AGE >= 0'.",
                }
            },
        },
    },
    {
        "name": "head_by_group",
        "summary": "Keep the top-N rows per group by an ordering (e.g. latest per key).",
        "category": "sampling",
        "requires_family": "spark",
        "params_schema": {
            "type": "object",
            "required": ["partition_by", "order_by", "n"],
            "additionalProperties": False,
            "properties": {
                "partition_by": {"type": "array", "minItems": 1, "items": {"type": "string"}},
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
        },
    },
    {
        "name": "limit",
        "summary": "Keep at most N rows.",
        "category": "sampling",
        "requires_family": None,
        "params_schema": {
            "type": "object",
            "required": ["n"],
            "additionalProperties": False,
            "properties": {"n": {"type": "integer", "minimum": 1}},
        },
    },
    {
        "name": "normalize_whitespace",
        "summary": "Collapse repeated whitespace to single spaces and trim the ends.",
        "category": "formatting",
        "requires_family": None,
        "params_schema": {
            "type": "object",
            "required": ["columns"],
            "additionalProperties": False,
            "properties": {
                "columns": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                    "description": "Columns to normalise (rewritten in place).",
                }
            },
        },
    },
    {
        "name": "null_if",
        "summary": "Set a column to null when its value matches a list, pattern, or bound.",
        "category": "correction",
        "requires_family": None,
        "params_schema": {
            "type": "object",
            "required": ["column"],
            "additionalProperties": False,
            "properties": {
                "column": {"type": "string"},
                "equals": {"type": "array", "items": {}},
                "pattern": {"type": "string"},
                "less_than": {"type": "number"},
                "greater_than": {"type": "number"},
            },
        },
    },
    {
        "name": "pad",
        "summary": "Pad a column to a fixed width with a fill character (left or right).",
        "category": "formatting",
        "requires_family": None,
        "params_schema": {
            "type": "object",
            "required": ["column", "width"],
            "additionalProperties": False,
            "properties": {
                "column": {"type": "string"},
                "width": {"type": "integer", "minimum": 1},
                "char": {"type": "string", "default": "0", "maxLength": 1},
                "side": {"type": "string", "enum": ["left", "right"], "default": "left"},
            },
        },
    },
    {
        "name": "pivot",
        "summary": "Pivot a key/value column pair into wide columns per key.",
        "category": "reshape",
        "requires_family": None,
        "params_schema": {
            "type": "object",
            "required": ["group_by", "pivot_column", "value_column"],
            "additionalProperties": False,
            "properties": {
                "group_by": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                "pivot_column": {"type": "string"},
                "value_column": {"type": "string"},
                "agg": {
                    "type": "string",
                    "enum": ["avg", "count", "first", "last", "max", "mean", "min", "sum"],
                    "default": "first",
                },
            },
        },
    },
    {
        "name": "random_sample",
        "summary": "Take a random fraction of rows (optionally seeded, reproducible).",
        "category": "sampling",
        "requires_family": None,
        "params_schema": {
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
        },
    },
    {
        "name": "rank",
        "summary": "Add a rank (with gaps for ties) within partitions.",
        "category": "reshape",
        "requires_family": "spark",
        "params_schema": {
            "type": "object",
            "required": ["order_by", "output_column"],
            "additionalProperties": False,
            "properties": {
                "partition_by": {"type": "array", "items": {"type": "string"}},
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
                "output_column": {"type": "string"},
            },
        },
    },
    {
        "name": "reduce",
        "summary": "Group by key columns and aggregate the rest (one row per group).",
        "category": "reshape",
        "requires_family": None,
        "params_schema": {
            "type": "object",
            "required": ["group_by", "aggs"],
            "additionalProperties": False,
            "properties": {
                "group_by": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                "aggs": {
                    "type": "object",
                    "minProperties": 1,
                    "additionalProperties": {
                        "type": "string",
                        "enum": ["avg", "count", "first", "last", "max", "mean", "min", "sum"],
                    },
                    "description": "{column: agg_func} \u2014 max/min/sum/avg/count/first/last.",
                },
            },
        },
    },
    {
        "name": "regex_replace",
        "summary": "Replace regex matches with a replacement string across columns.",
        "category": "formatting",
        "requires_family": None,
        "params_schema": {
            "type": "object",
            "required": ["columns", "pattern", "replacement"],
            "additionalProperties": False,
            "properties": {
                "columns": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                    "description": "Columns to apply the substitution to.",
                },
                "pattern": {"type": "string", "description": "Regex to match."},
                "replacement": {
                    "type": "string",
                    "description": "Replacement (may reference capture groups, e.g. $1).",
                },
            },
        },
    },
    {
        "name": "rename_columns",
        "summary": "Rename columns using an {old: new} mapping.",
        "category": "formatting",
        "requires_family": None,
        "params_schema": {
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
        },
    },
    {
        "name": "select_columns",
        "summary": "Keep only the listed columns, in the given order.",
        "category": "reshape",
        "requires_family": None,
        "params_schema": {
            "type": "object",
            "required": ["columns"],
            "additionalProperties": False,
            "properties": {
                "columns": {"type": "array", "minItems": 1, "items": {"type": "string"}}
            },
        },
    },
    {
        "name": "standardize_null",
        "summary": "Convert sentinel strings (NULL, NA, '.', empty) to true null.",
        "category": "correction",
        "requires_family": None,
        "params_schema": {
            "type": "object",
            "required": ["columns"],
            "additionalProperties": False,
            "properties": {
                "columns": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                "sentinels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Override the default sentinel list.",
                },
            },
        },
    },
    {
        "name": "stratified_sample",
        "summary": "Sample a per-key fraction of rows, preserving key distribution.",
        "category": "sampling",
        "requires_family": "spark",
        "params_schema": {
            "type": "object",
            "required": ["key", "fractions"],
            "additionalProperties": False,
            "properties": {
                "key": {"type": "string", "description": "Column to stratify on."},
                "fractions": {
                    "type": "object",
                    "minProperties": 1,
                    "additionalProperties": {"type": "number", "minimum": 0, "maximum": 1},
                    "description": "{key_value: fraction} map.",
                },
                "seed": {"type": "integer"},
            },
        },
    },
    {
        "name": "strip_non_alpha",
        "summary": "Remove all non-letter characters from one or more columns.",
        "category": "correction",
        "requires_family": None,
        "params_schema": {
            "type": "object",
            "required": ["columns"],
            "additionalProperties": False,
            "properties": {
                "columns": {"type": "array", "minItems": 1, "items": {"type": "string"}}
            },
        },
    },
    {
        "name": "strip_non_numeric",
        "summary": "Remove all non-digit characters from one or more columns.",
        "category": "correction",
        "requires_family": None,
        "params_schema": {
            "type": "object",
            "required": ["columns"],
            "additionalProperties": False,
            "properties": {
                "columns": {"type": "array", "minItems": 1, "items": {"type": "string"}}
            },
        },
    },
    {
        "name": "to_lower",
        "summary": "Lowercase the values in one or more columns.",
        "category": "formatting",
        "requires_family": None,
        "params_schema": {
            "type": "object",
            "required": ["columns"],
            "additionalProperties": False,
            "properties": {
                "columns": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                    "description": "Columns to case-normalise (rewritten in place).",
                }
            },
        },
    },
    {
        "name": "to_title",
        "summary": "Title-case (initcap) the values in one or more columns.",
        "category": "formatting",
        "requires_family": None,
        "params_schema": {
            "type": "object",
            "required": ["columns"],
            "additionalProperties": False,
            "properties": {
                "columns": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                    "description": "Columns to case-normalise (rewritten in place).",
                }
            },
        },
    },
    {
        "name": "to_upper",
        "summary": "Uppercase the values in one or more columns.",
        "category": "formatting",
        "requires_family": None,
        "params_schema": {
            "type": "object",
            "required": ["columns"],
            "additionalProperties": False,
            "properties": {
                "columns": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                    "description": "Columns to case-normalise (rewritten in place).",
                }
            },
        },
    },
    {
        "name": "trim",
        "summary": "Strip leading and trailing whitespace from one or more columns.",
        "category": "formatting",
        "requires_family": None,
        "params_schema": {
            "type": "object",
            "required": ["columns"],
            "additionalProperties": False,
            "properties": {
                "columns": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                    "description": "Columns to trim (rewritten in place).",
                }
            },
        },
    },
    {
        "name": "unpivot",
        "summary": "Melt wide value columns into long (name, value) rows.",
        "category": "reshape",
        "requires_family": None,
        "params_schema": {
            "type": "object",
            "required": ["id_columns", "value_columns", "name_to", "value_to"],
            "additionalProperties": False,
            "properties": {
                "id_columns": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                "value_columns": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                "name_to": {"type": "string"},
                "value_to": {"type": "string"},
            },
        },
    },
]


#: Valid category values, for UI grouping and filter validation.
CATEGORIES: tuple[str, ...] = ("correction", "formatting", "reshape", "sampling")


def list_catalogue(category: str | None = None) -> list[dict[str, Any]]:
    """Return catalogue entries, optionally filtered by ``category``."""
    if category is None:
        return [dict(e) for e in TRANSFORM_CATALOGUE]
    return [dict(e) for e in TRANSFORM_CATALOGUE if e["category"] == category]


def transform_names() -> list[str]:
    """Return the sorted list of catalogued transform names."""
    return [str(e["name"]) for e in TRANSFORM_CATALOGUE]


__all__ = [
    "CATEGORIES",
    "TRANSFORM_CATALOGUE",
    "list_catalogue",
    "transform_names",
]
