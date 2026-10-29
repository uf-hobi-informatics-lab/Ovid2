"""CI, the public-mirror publisher and contributors share one gate script.

GitHub Actions went red on the first public push because CI ran the full
ruff/mypy/pytest gates while ``publish.py`` only ran a subset. Both now
call ``scripts/ci_checks.sh``; these checks keep it that way:

* every gate job in ``.github/workflows/ci.yml`` invokes the script (no
  inline ``ruff`` / ``mypy`` / ``pytest`` commands that could drift);
* the script defines the four gates CI expects;
* the public-mirror manifest does not exclude the script (CI on GitHub
  needs it).
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github" / "workflows" / "ci.yml"
_SCRIPT = _ROOT / "scripts" / "ci_checks.sh"
_MANIFEST = _ROOT / "scripts" / "public" / "manifest.toml"
_GATES = {"lint": "lint", "typecheck": "typecheck", "contract": "contract", "unit": "unit"}


@pytest.mark.contract
def test_every_gate_job_calls_the_shared_script() -> None:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    for job_id, gate in _GATES.items():
        runs = [str(step.get("run", "")) for step in jobs[job_id]["steps"]]
        assert any(f"scripts/ci_checks.sh {gate}" in r for r in runs), (
            f"CI job '{job_id}' must run `bash scripts/ci_checks.sh {gate}`"
        )
        for r in runs:
            if "ci_checks.sh" in r or "pip install" in r:
                continue
            for tool in ("ruff ", "mypy ", "pytest "):
                assert tool not in r, f"CI job '{job_id}' runs `{tool.strip()}` inline: {r!r}"


@pytest.mark.contract
def test_script_defines_every_gate() -> None:
    text = _SCRIPT.read_text(encoding="utf-8")
    for gate in _GATES.values():
        assert f"        {gate})" in text, f"scripts/ci_checks.sh lacks the '{gate}' gate"
    assert _SCRIPT.stat().st_mode & 0o111, "scripts/ci_checks.sh must be executable"


@pytest.mark.contract
def test_public_mirror_ships_the_script() -> None:
    if not _MANIFEST.is_file():
        pytest.skip("public-mirror tooling not present")
    import tomllib

    manifest = tomllib.loads(_MANIFEST.read_text(encoding="utf-8"))
    rel = "scripts/ci_checks.sh"
    for pat in manifest["exclude"]["paths"]:
        base = pat[:-3] if pat.endswith("/**") else None
        hit = (base is not None and (rel == base or rel.startswith(base + "/"))) or (
            fnmatch.fnmatch(rel, pat)
        )
        assert not hit, f"manifest exclude {pat!r} would drop {rel} from the public mirror"
