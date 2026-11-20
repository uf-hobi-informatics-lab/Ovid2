"""Contract: every check's ``remediation`` resolves and severities are pinned.

A check's ``remediation.fixers`` / ``remediation.transforms`` are what the
repair-context endpoint and the assistant's ``get_repair_context`` tool
offer as candidate fixes, so each name must be a registered fixer
(``config.fixers_catalogue.FIXER_CATALOGUE``) or a transform op of the
public library (``config.transforms_catalogue``). A typo would make the
assistant propose a fixer that does not exist.

Also pins that every core check declares an explicit ``severity`` (the
repair context orders failures by it) and that ``applies_to_cdms`` only
names registered CDMs.

ADR: the adr-domain-packs design note.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_CHECKS = Path(__file__).resolve().parents[2] / "src" / "ovid2" / "resources" / "health" / "checks"


def _specs() -> list[tuple[str, dict[str, object]]]:
    return [
        (p.name, yaml.safe_load(p.read_text(encoding="utf-8")))
        for p in sorted(_CHECKS.glob("*.yaml"))
    ]


@pytest.mark.contract
def test_remediation_names_resolve() -> None:
    from ovid2.config.fixers_catalogue import FIXER_CATALOGUE
    from ovid2.config.transforms_catalogue import transform_names

    transforms = set(transform_names())
    offenders: list[str] = []
    for name, raw in _specs():
        rem = raw.get("remediation") or {}
        assert isinstance(rem, dict), name
        for fixer in rem.get("fixers") or []:
            if fixer not in FIXER_CATALOGUE:
                offenders.append(f"{name}: fixer {fixer!r}")
        for op in rem.get("transforms") or []:
            if op not in transforms:
                offenders.append(f"{name}: transform {op!r}")
    assert not offenders, "remediation names do not resolve:\n" + "\n".join(offenders)


@pytest.mark.contract
def test_core_checks_declare_severity() -> None:
    from ovid2.transformations.quality.registry import SEVERITIES

    offenders = [n for n, raw in _specs() if raw.get("severity") not in SEVERITIES]
    assert not offenders, "core checks without an explicit severity:\n" + "\n".join(offenders)


@pytest.mark.contract
def test_applies_to_cdms_are_registered() -> None:
    import ovid2.cdm  # noqa: F401  (registers the core CDMs)
    from ovid2.cdm.registry import registered_cdms

    known = set(registered_cdms())
    offenders = [
        f"{n}: {cdm!r}"
        for n, raw in _specs()
        for cdm in raw.get("applies_to_cdms") or []
        if cdm not in known
    ]
    assert not offenders, "applies_to_cdms names unknown CDMs:\n" + "\n".join(offenders)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
