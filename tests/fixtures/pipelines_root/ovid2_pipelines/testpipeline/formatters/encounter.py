"""Python-factory spec for tests (same shape as the YAML variant)."""

from __future__ import annotations

from ovid2.formatters.spec import spec

SPEC = spec(
    formatter="encounter",
    adapter="pcornet_passthrough",
    sources={
        "encounter": {
            "glob": "*Encounter*",
            "lowercase_columns": True,
        },
    },
    projections={
        "ENCOUNTERID": "encounterid",
        "PATID": "patid",
    },
)
