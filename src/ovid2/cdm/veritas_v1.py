"""Ovid2 pipeline-family CDM: Veritas.

Veritas pipeline feeds (Veritas-shaped feeds) do not conform to any published
CDM — they are a small, Ovid-internal shape used by one data source.
This handle registers just enough for the ETL engine to run against
that shape without leaking PCORnet assumptions.

The composite keys are intentionally empty at P10 landing time; they
will be filled in as each Veritas table's dedup requirements are
documented. Fixer chains referencing PCORnet-scoped fixers on Veritas
pipelines will fail validation (see ``test_fixer_cdm_scope``).
"""

from __future__ import annotations

import contextlib

from ovid2.cdm.handle import CdmHandle
from ovid2.cdm.registry import register

VERITAS_V1: CdmHandle = CdmHandle(
    name="ovid2_pipeline_veritas_v1",
    version="1.0",
    composite_keys={},
    reference_tables=frozenset(),
    id_columns={},
    flavour_of_null=frozenset({"", None}),
    shared_views=(),
    schema_source=None,
    schema_loader=None,
)


def _register_default() -> None:
    """Idempotently register the Veritas handle."""
    with contextlib.suppress(ValueError):
        register(VERITAS_V1)


_register_default()
