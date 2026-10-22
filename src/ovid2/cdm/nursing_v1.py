"""Ovid2 pipeline-family CDM: Nursing.

Nursing pipeline feeds (nursing-shaped feeds) use OMOP-shaped
table names (``condition_occurrence``, ``procedure_occurrence``,
``fact_relationship``) but do not run through the full OMOP concept-join
adapter. This handle registers the shape without pulling in OMOP
vocabulary shared views unconditionally.

Composite keys will be filled in as the nursing mapper family is
authored — leaving them empty at P10 landing time is deliberate so the
mapper spec engine can validate against a real, if minimal, contract.
"""

from __future__ import annotations

from ovid2.cdm.handle import CdmHandle
from ovid2.cdm.registry import register

NURSING_V1: CdmHandle = CdmHandle(
    name="ovid2_pipeline_nursing_v1",
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
    """Idempotently register the Nursing handle."""
    try:
        register(NURSING_V1)
    except ValueError:
        pass


_register_default()
