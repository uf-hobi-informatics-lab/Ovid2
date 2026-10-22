"""Synthetic minimal CDM fixture for P10 integration tests.

Registers ``pcornet_lite_1_0`` — a five-table, ten-field CDM used to
prove the CDM/ETL independence contract. The fixture is registered
in-process by tests that import it; it does NOT self-register at
module import time (unlike production CDMs) so that unit tests can
exercise the registry in isolation.

Every value in this handle is deliberately different from the
production PCORnet handle so a test that accidentally reads the wrong
handle produces an obvious mismatch.
"""

from __future__ import annotations

from ovid2.cdm.handle import CdmHandle, IdColumn

PCORNET_LITE_1_0: CdmHandle = CdmHandle(
    name="pcornet_lite_1_0",
    version="1.0-test",
    composite_keys={
        "demographic": ("PATID",),
        "encounter": ("ENCOUNTERID",),
        "diagnosis": ("ENCOUNTERID", "DX"),
        "provider": ("PROVIDERID",),
        "harvest": ("DATAMARTID",),
    },
    reference_tables=frozenset({"demographic", "encounter", "provider", "harvest"}),
    id_columns={
        "patient": IdColumn(role="patient", column="PATID", width=32, reference_table="demographic"),
        "encounter": IdColumn(role="encounter", column="ENCOUNTERID", width=48, reference_table="encounter"),
        "provider": IdColumn(role="provider", column="PROVIDERID", width=32, reference_table="provider"),
    },
    flavour_of_null=frozenset({"NI", "", None}),
    shared_views=(),
    schema_source=None,
    schema_loader=None,
)
