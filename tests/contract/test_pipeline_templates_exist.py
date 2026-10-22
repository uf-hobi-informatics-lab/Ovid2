"""Contract: every pipeline template points at an installed pipeline (P3).

The curated pipeline templates (:mod:`ovid2.tools.templates`) each name
a ``source_pipeline`` that the scaffolder clones and a target ``cdm``
that must be registered. If a template's source pipeline is deleted or
renamed — or its CDM removed — the scaffolder would fail at runtime;
this contract catches that at commit time.
"""

from __future__ import annotations

import importlib

import pytest

from ovid2.cdm import registered_cdms
from ovid2.tools.templates import list_templates
from ovid2.web.models import PIPELINE_KINDS


@pytest.mark.contract
def test_template_source_pipelines_installed() -> None:
    offenders: list[str] = []
    for t in list_templates():
        try:
            mod = importlib.import_module(f"ovid2_pipelines.{t.source_pipeline}")
        except ModuleNotFoundError:
            offenders.append(f"{t.id}: source '{t.source_pipeline}' not installed")
            continue
        if not getattr(mod, "__path__", None):
            offenders.append(f"{t.id}: source '{t.source_pipeline}' has no path")
    assert not offenders, "Template source issues:\n  " + "\n  ".join(offenders)


@pytest.mark.contract
def test_template_cdms_registered() -> None:
    known = set(registered_cdms())
    offenders = [
        f"{t.id}: cdm '{t.cdm}' not registered"
        for t in list_templates()
        if t.cdm not in known
    ]
    assert not offenders, "Template CDM issues:\n  " + "\n  ".join(offenders)


@pytest.mark.contract
def test_template_kinds_are_valid() -> None:
    offenders = [
        f"{t.id}: kind '{t.kind}' not in PIPELINE_KINDS"
        for t in list_templates()
        if t.kind not in PIPELINE_KINDS
    ]
    assert not offenders, "Template kind issues:\n  " + "\n  ".join(offenders)
