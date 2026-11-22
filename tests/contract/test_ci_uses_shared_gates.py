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


_BITBUCKET = _ROOT / "bitbucket-pipelines.yml"
_HEAVY = _ROOT / ".github" / "workflows" / "heavy.yml"
_HEAVY_GATES = ("integration", "io_matrix")


def _bitbucket_steps() -> list[dict[str, object]]:
    data = yaml.safe_load(_BITBUCKET.read_text(encoding="utf-8"))
    steps: list[dict[str, object]] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            if "step" in node and isinstance(node["step"], dict):
                steps.append(node["step"])
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(data["pipelines"])
    return steps


@pytest.mark.contract
def test_bitbucket_steps_call_the_shared_script() -> None:
    """Every Bitbucket gate step runs ``scripts/ci_checks.sh <gate>`` — no inline tools."""
    if not _BITBUCKET.is_file():
        pytest.skip("bitbucket-pipelines.yml not present (public mirror)")
    gates_seen: set[str] = set()
    for step in _bitbucket_steps():
        script = [str(line) for line in step.get("script", [])]  # type: ignore[union-attr]
        for line in script:
            for gate in (*_GATES, *_HEAVY_GATES):
                if f"scripts/ci_checks.sh {gate}" in line:
                    gates_seen.add(gate)
            if "ci_checks.sh" in line or "pip install" in line:
                continue
            for tool in ("ruff ", "mypy ", "pytest "):
                assert tool not in line, f"Bitbucket step {step.get('name')!r} runs {tool!r} inline"
    assert gates_seen >= {*_GATES, *_HEAVY_GATES}, gates_seen


@pytest.mark.contract
def test_heavy_gates_are_change_triggered_not_nightly() -> None:
    """Heavy gates run on changesets / path filters; Bitbucket has no cron-ish default."""
    if not _BITBUCKET.is_file():
        pytest.skip("bitbucket-pipelines.yml not present (public mirror)")
    data = yaml.safe_load(_BITBUCKET.read_text(encoding="utf-8"))
    default_heavy = [
        b["step"]
        for block in data["pipelines"]["default"]
        for b in block.get("parallel", {}).get("steps", [])
        if any(
            "ci_checks.sh integration" in str(x) or "ci_checks.sh io_matrix" in str(x)
            for x in b["step"]["script"]
        )
    ]
    assert len(default_heavy) == 2
    bb_paths = [set(s["condition"]["changesets"]["includePaths"]) for s in default_heavy]
    assert bb_paths[0] == bb_paths[1]
    gh = yaml.safe_load(_HEAVY.read_text(encoding="utf-8"))[True]
    gh_paths = set(gh["push"]["paths"]) - {".github/workflows/heavy.yml"}
    assert gh_paths == bb_paths[0] - {"bitbucket-pipelines.yml"}, "heavy-path lists drifted"
    assert set(gh["pull_request"]["paths"]) == set(gh["push"]["paths"])


@pytest.mark.contract
def test_script_defines_heavy_gates() -> None:
    text = _SCRIPT.read_text(encoding="utf-8")
    for gate in _HEAVY_GATES:
        assert f"        {gate})" in text, f"scripts/ci_checks.sh lacks the '{gate}' gate"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
