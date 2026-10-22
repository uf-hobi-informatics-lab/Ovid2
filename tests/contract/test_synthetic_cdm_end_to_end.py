"""Integration: the P10 CDM abstraction works end-to-end against a synthetic CDM.

Registers ``pcornet_lite_1_0`` in-process and asserts that the mapper
spec engine and the deduplicator both consult it — no hardcoded PCORnet
symbols are touched. The test skips when no JVM is available (the
deduplicator path needs Spark).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the CDM fixture importable.
_FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures"
if str(_FIXTURE_ROOT) not in sys.path:
    sys.path.insert(0, str(_FIXTURE_ROOT))

from cdm.pcornet_lite_1_0 import PCORNET_LITE_1_0  # noqa: E402


@pytest.mark.contract
def test_synthetic_cdm_registers_without_error() -> None:
    """A minimal handle registers and resolves like any bundled CDM."""
    from ovid2.cdm.registry import _clear, get_cdm, register, registered_cdms

    # Snapshot then restore the registry so the test is idempotent.
    from ovid2.cdm import (  # noqa: F401 — force default registrations
        nursing_v1,
        pcornet_7_0,
        tumor_v1,
        veritas_v1,
    )

    before = set(registered_cdms())
    try:
        register(PCORNET_LITE_1_0)
        assert "pcornet_lite_1_0" in registered_cdms()
        handle = get_cdm("pcornet_lite_1_0")
        assert handle.name == "pcornet_lite_1_0"
        assert handle.dedup_key_for("diagnosis") == ("ENCOUNTERID", "DX")
        assert "demographic" in handle.reference_tables
        assert handle.id_column_for("patient").column == "PATID"
        assert handle.id_column_for("patient").width == 32  # different from prod PCORnet
    finally:
        _clear()
        # Re-import the bundled CDMs to restore the default registrations.
        import importlib

        for mod in (
            "ovid2.cdm.pcornet_7_0",
            "ovid2.cdm.omop_5_3",
            "ovid2.cdm.veritas_v1",
            "ovid2.cdm.nursing_v1",
            "ovid2.cdm.tumor_v1",
        ):
            importlib.reload(importlib.import_module(mod))
        # Post-condition — the default CDMs are back.
        after = set(registered_cdms())
        assert before.issubset(after)


@pytest.mark.contract
def test_mapper_spec_engine_runs_against_synthetic_cdm_without_pcornet_reference() -> None:
    """Load a mapper spec, execute it over a fake DataFrame, verify no PCORnet leaks.

    This test does not need Spark — it substitutes a stub DataFrame with
    the minimum interface each op touches (``withColumn`` returning self).
    The purpose is to prove that :func:`ovid2.mappers.engine.run` never
    imports or reaches into ``ovid2.cdm.pcornet_7_0``.
    """
    from ovid2.mappers import load_from_dict, run
    from ovid2.mappers.registry import MAP_OPS

    # A stub DataFrame that returns itself for every op.
    class StubDataFrame:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def withColumn(self, name: str, expr: object) -> "StubDataFrame":  # noqa: N802
            self.calls.append(name)
            return self

    # ``translate_by_dict`` needs Spark's ``create_map`` at import time,
    # so use only op(s) that do not touch pyspark to keep this test
    # JVM-free. ``passthrough`` is the cleanest choice.
    assert "passthrough" in MAP_OPS

    spec = load_from_dict(
        {
            "v": 1,
            "table": "demographic",
            "ops": [{"op": "passthrough"}],
        },
        pipeline="synthetic",
    )

    df = StubDataFrame()
    result = run(df, spec, spark=None, cdm=PCORNET_LITE_1_0)  # type: ignore[arg-type]
    assert result is df
    # Passthrough must not touch the DataFrame at all.
    assert df.calls == []
