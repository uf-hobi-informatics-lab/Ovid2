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
import tomllib
from pathlib import Path

import pytest

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


def test_leak_gate_allow_entries_are_exact_and_live() -> None:
    """``[[leak_gate.allow]]`` exceptions are line-scoped and never stale.

    Each entry must name one concrete file (no glob characters), that file
    must exist, and its ``line`` regex must still match at least one line —
    and that line must actually trip the denylist (otherwise the exception
    is unnecessary and should be removed).
    """
    gate = _manifest()["leak_gate"]
    deny = [re.compile(p, re.IGNORECASE) for p in gate["deny"]]
    problems = []
    for entry in gate.get("allow", []):
        rel = entry["path"]
        if any(ch in rel for ch in "*?["):
            problems.append(f"{rel}: allow path must be an exact file, not a glob")
            continue
        target = _ROOT / rel
        if not target.is_file():
            problems.append(f"{rel}: allow path does not exist")
            continue
        line_re = re.compile(entry["line"])
        hits = [ln for ln in target.read_text(encoding="utf-8").splitlines() if line_re.search(ln)]
        if not hits:
            problems.append(f"{rel}: allow line {entry['line'][:60]!r} matches nothing")
        elif not any(p.search(ln) for ln in hits for p in deny):
            problems.append(f"{rel}: allow line matches but trips no deny rule (remove it)")
    assert not problems, "manifest [[leak_gate.allow]] problems:\n  " + "\n  ".join(problems)


def test_pre_push_hook_runs_publish_on_python_311_plus() -> None:
    """The pre-push hook must not run ``publish.py`` on a bare ``python3``.

    ``publish.py`` imports ``tomllib`` (3.11+); a host whose system
    ``python3`` is older crashed every background publish. The hook picks a
    3.11+ interpreter into ``$publish_python`` and uses it everywhere.
    """
    hook = (_ROOT / "scripts" / "git-hooks" / "pre-push").read_text(encoding="utf-8")
    assert "sys.version_info < (3, 11)" in hook
    assert '"$publish_python" scripts/public/publish.py' in hook
    for line in hook.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not re.search(r"(^|[\s;&(])python3\s+(-\s|scripts/)", stripped), (
            f"pre-push invokes bare python3: {stripped!r}"
        )
