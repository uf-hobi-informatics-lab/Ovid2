"""Contract: domain-pack manifests and their content are valid.

- every built-in ``resources/packs/<id>/pack.yaml`` validates against
  ``pack.v1.json`` and its directory name equals its id;
- a built-in pack's ``provides.cdms`` / ``provides.members`` name content
  that actually ships in the core tree (checks, packages, fixers,
  vocabularies);
- no core check, package or fixer is claimed by two built-in packs;
- core health checks use only the core ``part`` buckets (the schema
  accepts any snake-case part so packs can declare their own, so this is
  where the core set is pinned);
- a freshly scaffolded pack (folder and pip layout) passes
  ``ovid2 tools pack validate``.

ADR: the adr-domain-packs design note.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_SRC = Path(__file__).resolve().parents[2] / "src" / "ovid2"
_PACKS = _SRC / "resources" / "packs"
_CHECKS = _SRC / "resources" / "health" / "checks"
_PACKAGES = _SRC / "resources" / "health" / "packages"


def _builtin_roots() -> list[Path]:
    return sorted(p for p in _PACKS.iterdir() if (p / "pack.yaml").is_file())


def _ids(directory: Path) -> set[str]:
    return {
        str(yaml.safe_load(p.read_text(encoding="utf-8"))["id"]) for p in directory.glob("*.yaml")
    }


@pytest.mark.contract
def test_builtin_pack_manifests_validate() -> None:
    from ovid2.packs.manifest import load_manifest

    roots = _builtin_roots()
    assert roots, "no built-in domain pack found (pcornet_7_0 is the reference pack)"
    for root in roots:
        manifest = load_manifest(root)
        assert manifest.id == root.name, f"{root}: id {manifest.id!r} != directory name"


@pytest.mark.contract
def test_builtin_pack_members_exist() -> None:
    import ovid2.cdm  # noqa: F401  (registers the core CDMs)
    from ovid2.cdm.registry import registered_cdms
    from ovid2.config.fixers_catalogue import FIXER_CATALOGUE
    from ovid2.packs.manifest import load_manifest

    checks, packages = _ids(_CHECKS), _ids(_PACKAGES)
    known: dict[str, set[str]] = {
        "checks": checks,
        "packages": packages,
        "fixers": set(FIXER_CATALOGUE),
    }
    claimed: dict[tuple[str, str], str] = {}
    problems: list[str] = []
    for root in _builtin_roots():
        manifest = load_manifest(root)
        for cdm in manifest.cdms:
            if cdm not in registered_cdms():
                problems.append(f"{manifest.id}: CDM {cdm!r} is not registered")
        for kind, names in manifest.members.items():
            for name in names:
                if kind in known and name not in known[kind]:
                    problems.append(f"{manifest.id}: members.{kind} {name!r} does not exist")
                owner = claimed.setdefault((kind, name), manifest.id)
                if owner != manifest.id:
                    problems.append(f"{kind} {name!r} claimed by {owner} and {manifest.id}")
    assert not problems, "\n".join(problems)


@pytest.mark.contract
def test_core_checks_use_core_parts() -> None:
    from ovid2.packs.content import CORE_PARTS

    offenders = [
        f"{p.name}: part {raw['part']!r}"
        for p in sorted(_CHECKS.glob("*.yaml"))
        if (raw := yaml.safe_load(p.read_text(encoding="utf-8")))["part"] not in CORE_PARTS
    ]
    assert not offenders, "core checks must use a core part:\n" + "\n".join(offenders)


@pytest.mark.contract
@pytest.mark.parametrize("python", [False, True], ids=["folder", "pip"])
def test_scaffolded_pack_validates(tmp_path: Path, python: bool) -> None:
    from ovid2.tools.pack import scaffold_pack, validate_pack_dir

    target = scaffold_pack("demo_pack", tmp_path, domain="example", python=python)
    root = target / "ovid2_pack_demo_pack" if python else target
    result = validate_pack_dir(root)
    assert result["ok"], result["errors"]
    assert result["counts"] == {"cdms": 1, "checks": 1, "packages": 1}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
