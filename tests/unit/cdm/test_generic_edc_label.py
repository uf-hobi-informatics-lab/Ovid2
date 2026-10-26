"""Unit tests for the generic package-free EDC labels.

The reusable data-health / EDC-checks surfaces run against *any* database
of a CDM by using a well-known label (``pcornet_edc``) instead of a
per-partner ``ovid2_pipelines.<name>`` package. These tests lock in that:

1. the label resolves its CDM directly (no package on disc), and
2. it never falls through to a partner package's
   ``DEFAULT_PIPELINE_DB_NAME`` — the database always comes from the
   run-time-selected address.
"""

from __future__ import annotations

import pytest

# Import the package for its self-registration side effect.
import ovid2.cdm  # noqa: F401
from ovid2.cdm.registry import (
    GENERIC_CDM_LABELS,
    resolve_cdm_for_pipeline,
)


@pytest.mark.unit
def test_pcornet_edc_is_a_generic_label() -> None:
    assert "pcornet_edc" in GENERIC_CDM_LABELS
    assert GENERIC_CDM_LABELS["pcornet_edc"] == "pcornet_7_0"


@pytest.mark.unit
def test_generic_label_resolves_cdm_without_package() -> None:
    """``pcornet_edc`` resolves to PCORnet 7.0 with no package installed."""
    handle = resolve_cdm_for_pipeline("pcornet_edc")
    assert handle.name == "pcornet_7_0"


@pytest.mark.unit
def test_generic_label_ignores_a_same_named_package(tmp_path, monkeypatch) -> None:
    """Even if a package named ``pcornet_edc`` existed, the label wins.

    The generic label is deliberately decoupled from the filesystem: a
    stray ``ovid2_pipelines.pcornet_edc`` package must not be able to
    hijack the CDM (which would silently re-couple the reusable run to a
    package).
    """
    import sys

    root = tmp_path / "pipelines_root"
    pkg = root / "ovid2_pipelines" / "pcornet_edc"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    # A misleading package that binds a *different* CDM.
    (pkg / "pipeline_settings.py").write_text(
        "cdm = 'omop_5_3'\nDEFAULT_PIPELINE_DB_NAME = 'SHOULD_NOT_WIN'\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(root))
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]
    try:
        handle = resolve_cdm_for_pipeline("pcornet_edc")
        assert handle.name == "pcornet_7_0"
    finally:
        for mod in list(sys.modules):
            if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
                del sys.modules[mod]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run via pytest.")
