"""Web-safe catalogue of mapper ops with parameter schemas.

The pipeline builder renders a form per mapper op, and the web / MCP tiers
may not import :mod:`ovid2.mappers` (``test_web_no_pipeline_imports.py``).
This module mirrors :data:`ovid2.mappers.registry.MAP_OPS` with a one-line
summary and a JSON-schema ``params_schema`` per op (derived from each op's
documented ``Params:``). ``tests/contract/test_mapper_ops_catalogue_synchronised.py``
fails when the names drift from the registry.

``legacy_select`` / ``apply_transforms`` have nested shapes the form edits
as YAML; their schema is permissive (``additionalProperties: true``).
"""

from __future__ import annotations

from typing import Any

__all__ = ["MAPPER_OP_CATALOGUE", "MAPPER_OP_COLUMN_ROLES", "list_mapper_ops"]

_STR = {"type": "string"}

MAPPER_OP_CATALOGUE: dict[str, dict[str, Any]] = {
    "translate_by_dict": {
        "summary": "Substitute a column's values from a dictionaries.py dict.",
        "params_schema": {
            "type": "object",
            "required": ["column", "dict"],
            "additionalProperties": False,
            "properties": {
                "column": {**_STR, "description": "Column to translate."},
                "dict": {
                    **_STR,
                    "description": "Name of a dict in dictionaries.py (<table>_<column>_dict).",
                },
                "default": {"description": "Fallback literal for unmatched values."},
                "keep_null": {"type": "boolean", "default": True},
            },
        },
    },
    "translate_by_concept": {
        "summary": "Translate a column through a registered vocabulary (broadcast join).",
        "params_schema": {
            "type": "object",
            "required": ["column", "vocabulary"],
            "additionalProperties": False,
            "properties": {
                "column": _STR,
                "vocabulary": {**_STR, "description": "Registered vocabulary, e.g. omop_concept."},
                "match": {**_STR, "description": "Vocabulary column to match (default code)."},
                "target": {**_STR, "description": "Vocabulary column written back."},
                "target_column": {**_STR, "description": "Output column (default: column)."},
                "keep_unmatched": {"type": "boolean", "default": True},
                "domain_filter": {"type": "array", "items": _STR},
                "case_sensitive": {"type": "boolean", "default": False},
            },
        },
    },
    "hash_id": {
        "summary": "Replace an identifier with a salted SHA-256 (salt from secrets.d).",
        "params_schema": {
            "type": "object",
            "required": ["column"],
            "additionalProperties": False,
            "properties": {
                "column": _STR,
                "salt_key": {
                    **_STR,
                    "default": "id_salt",
                    "description": "Key in secrets.d/<pipeline>.json.",
                },
                "algorithm": {"type": "string", "enum": ["sha256"], "default": "sha256"},
                "output_length": {"type": "integer", "minimum": 1},
            },
        },
    },
    "case_map": {
        "summary": "Upper / lower / title-case a column.",
        "params_schema": {
            "type": "object",
            "required": ["column"],
            "additionalProperties": False,
            "properties": {
                "column": _STR,
                "mode": {"type": "string", "enum": ["upper", "lower", "title"], "default": "upper"},
            },
        },
    },
    "regex_replace": {
        "summary": "Regex substitution on a column.",
        "params_schema": {
            "type": "object",
            "required": ["column", "pattern"],
            "additionalProperties": False,
            "properties": {
                "column": _STR,
                "pattern": {**_STR, "description": "Java regex."},
                "replacement": {**_STR, "default": ""},
            },
        },
    },
    "literal": {
        "summary": "Set a column to a constant.",
        "params_schema": {
            "type": "object",
            "required": ["column"],
            "additionalProperties": False,
            "properties": {"column": _STR, "value": {"description": "Any JSON scalar."}},
        },
    },
    "passthrough": {
        "summary": "No-op (keeps the frame unchanged).",
        "params_schema": {"type": "object", "additionalProperties": False, "properties": {}},
    },
    "apply_transforms": {
        "summary": "Run a transform chain (named spec or inline steps) inside the mapper.",
        "params_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "spec": {**_STR, "description": "Named transform chain."},
                "steps": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Inline transform steps.",
                },
            },
        },
    },
    "legacy_select": {
        "summary": "Legacy-port column projection (generated; edit via YAML).",
        "params_schema": {"type": "object", "additionalProperties": True},
    },
}


#: Which columns each mapper op touches (grammar: :mod:`ovid2.config.column_roles`).
#: ``apply_transforms`` and ``legacy_select`` are expanded by the lineage
#: service (nested steps / per-column projection), so they are table-scoped here.
MAPPER_OP_COLUMN_ROLES: dict[str, dict[str, Any]] = {
    "translate_by_dict": {"scope": "field", "inplace": ["column"]},
    "translate_by_concept": {
        "scope": "field",
        "reads": ["column"],
        "writes": ["target_column|column"],
    },
    "hash_id": {"scope": "field", "inplace": ["column"]},
    "case_map": {"scope": "field", "inplace": ["column"]},
    "regex_replace": {"scope": "field", "inplace": ["column"]},
    "literal": {"scope": "field", "writes": ["column"]},
    "passthrough": {"scope": "table"},
    "apply_transforms": {"scope": "table"},
    "legacy_select": {"scope": "shape"},
}


def list_mapper_ops() -> list[dict[str, Any]]:
    """Return ``[{name, summary, params_schema}]`` sorted by name."""
    return [{"name": n, **e} for n, e in sorted(MAPPER_OP_CATALOGUE.items())]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.config.mapper_ops_catalogue is a library module.")
