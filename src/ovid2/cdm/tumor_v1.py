"""Ovid2 pipeline-family CDM: Tumor Registry.

Tumor Registry feeds (tumor-registry feeds) use a shape derived from
NAACCR-style tumor abstracts, not any published clinical CDM. This
handle registers the identifier and flavour-of-null pieces so the
generic ETL engine can drive tumor pipelines without PCORnet assumptions
leaking into their pipeline.

Composite keys are empty at P10 landing time; they will be added as
each tumor table's dedup requirements are documented.
"""

from __future__ import annotations

import contextlib

from ovid2.cdm.handle import CdmHandle
from ovid2.cdm.registry import register

TUMOR_V1: CdmHandle = CdmHandle(
    name="ovid2_pipeline_tumor_v1",
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
    """Idempotently register the Tumor handle."""
    with contextlib.suppress(ValueError):
        register(TUMOR_V1)


_register_default()
