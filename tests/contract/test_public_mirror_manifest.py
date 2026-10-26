"""Contract: the public-mirror tooling stays consistent with the tree.

The public GitHub mirror is built by ``scripts/public/publish.py`` from
``scripts/public/manifest.toml``. These checks catch drift *before* a push
fails the publish:

* the generated READMEs are up to date with ``scripts/public/readme/*.md``;
* every ``[[replace]]`` rule still finds its ``old`` text (otherwise a
  site-specific default would silently leak or the publish would abort);
* every pipeline package on disk is either excluded or explicitly public;
* the public overlay is itself free of denylisted terms.

These tests are skipped in the public tree, where ``scripts/public/`` is
not shipped.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

_ROOT = Path(__file__).resolve().parents[2]
_PUBLIC = _ROOT / "scripts" / "public"
_MANIFEST = _PUBLIC / "manifest.toml"

#: Pipeline packages that are intentionally published.
_PUBLIC_PIPELINES = {"example_pcornet", "pcornet_to_omop"}

pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(not _MANIFEST.is_file(), reason="public-mirror tooling not present"),
]


def _manifest() -> dict:
    return tomllib.loads(_MANIFEST.read_text(encoding="utf-8"))


def test_readmes_are_generated_from_fragments() -> None:
    proc = subprocess.run(
        [sys.executable, str(_PUBLIC / "build_readme.py"), "--check"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        "README.md or scripts/public/overlay/README.md is stale — edit "
        "scripts/public/readme/*.md and run scripts/public/build_readme.py\n" + proc.stdout
    )


def test_replace_rules_still_match() -> None:
    missing = []
    for rule in _manifest().get("replace", []):
        target = _ROOT / rule["path"]
        if not target.is_file() or rule["old"] not in target.read_text(encoding="utf-8"):
            missing.append(f"{rule['path']}: {rule['old'][:60]!r}")
    assert not missing, "manifest [[replace]] rules no longer match:\n  " + "\n  ".join(missing)


def test_every_pipeline_is_excluded_or_public() -> None:
    excluded = set()
    for pat in _manifest()["exclude"]["paths"]:
        m = re.fullmatch(r"pipelines/ovid2_pipelines/([A-Za-z0-9_]+)/\*\*", pat)
        if m:
            excluded.add(m.group(1))
    on_disk = {
        p.name
        for p in (_ROOT / "pipelines" / "ovid2_pipelines").iterdir()
        if p.is_dir() and not p.name.startswith(("_", "."))
    }
    unclassified = sorted(on_disk - excluded - _PUBLIC_PIPELINES)
    assert not unclassified, (
        "New pipeline package(s) not classified for the public mirror: "
        f"{unclassified}. Add 'pipelines/ovid2_pipelines/<name>/**' to "
        "[exclude] in scripts/public/manifest.toml, or add it to "
        "_PUBLIC_PIPELINES here if it is genuinely site-neutral."
    )
    gate = set(_manifest()["leak_gate"]["deny_pipeline_imports"])
    not_gated = sorted(excluded - gate)
    assert not not_gated, (
        f"Excluded pipelines must also be listed in [leak_gate] deny_pipeline_imports: {not_gated}"
    )


def test_overlay_is_leak_free() -> None:
    deny = [re.compile(p, re.IGNORECASE) for p in _manifest()["leak_gate"]["deny"]]
    hits = []
    for path in sorted((_PUBLIC / "overlay").rglob("*")):
        if not path.is_file():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for pattern in deny:
                if pattern.search(line):
                    hits.append(f"{path.relative_to(_ROOT)}:{lineno}: {pattern.pattern}")
    assert not hits, "public overlay contains denylisted terms:\n  " + "\n  ".join(hits)
