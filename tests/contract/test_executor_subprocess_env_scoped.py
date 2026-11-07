"""Contract: every executor subprocess the control plane spawns is scoped.

Invariants (``adr-harness-api-executor-trust-model.md``, "Control plane
vs data plane"):

1. Every ``subprocess.run`` / ``subprocess.Popen`` /
   ``asyncio.create_subprocess_exec`` call in ``src/ovid2/web/**`` and
   ``src/ovid2/mcp/**`` passes an explicit ``env=``. Inheriting the web
   process's environment hands the child its tokens and every ambient
   credential. (Tool-only calls that never touch Ovid2 config —
   ``docker image inspect`` etc. — are listed in :data:`_EXEMPT` with a
   reason.)
2. Every ``run_tool_json(...)`` broker call passes ``scope=``.
3. The docker launcher's secret scope covers every address / connection
   dest of every executor CLI parser — so adding ``--foo_address`` to a
   CLI without teaching the scoper fails here, not in production.
4. The worker's job scope covers the address / connection keys a job
   can carry.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

import pytest

#: (relative path, function name) → reason. Keep this tiny.
_EXEMPT: dict[tuple[str, str], str] = {
    ("web/services/engines_svc.py", "_image_present_locally"): (
        "`docker image inspect <tag>` — reads no Ovid2 config or secret"
    ),
    ("web/services/install_svc.py", "_run_build_process"): (
        "`docker build` of the repo — runs no Ovid2 code on the host"
    ),
    ("web/worker.py", "_run"): (
        "`docker stop` / `docker compose down` of a cancelled run — no Ovid2 code"
    ),
}

#: ``subprocess.<attr>`` / ``asyncio.<attr>`` callables that spawn a child.
_SPAWNERS = {
    "subprocess": {"run", "Popen", "call", "check_call", "check_output"},
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
}


def _enclosing_functions(tree: ast.AST) -> dict[int, str]:
    """Map each line to its *innermost* enclosing function name."""
    out: dict[int, str] = {}
    spans: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            spans.append((node.lineno, node.end_lineno or node.lineno, node.name))
    # Outer spans first, inner spans overwrite.
    for start, end, name in sorted(spans, key=lambda t: (t[0], -t[1])):
        for line in range(start, end + 1):
            out[line] = name
    return out


def _is_spawn(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.attr in _SPAWNERS.get(func.value.id, set())
    return False


@pytest.mark.contract
def test_control_plane_subprocesses_pass_env(src_root: Path) -> None:
    """Every spawn in web/ and mcp/ passes ``env=`` (or is explicitly exempt)."""
    offenders: list[str] = []
    for tier in ("web", "mcp"):
        for py in sorted((src_root / tier).rglob("*.py")):
            rel = str(py.relative_to(src_root))
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=rel)
            funcs = _enclosing_functions(tree)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and _is_spawn(node)):
                    continue
                if any(kw.arg == "env" for kw in node.keywords):
                    continue
                if (rel, funcs.get(node.lineno, "")) in _EXEMPT:
                    continue
                offenders.append(f"{rel}:{node.lineno} ({funcs.get(node.lineno, '?')})")
    assert not offenders, (
        "Control-plane subprocess spawned without an explicit env= "
        "(use ovid2.config.executor_env.scoped_executor_env):\n" + "\n".join(offenders)
    )


@pytest.mark.contract
def test_broker_calls_declare_scope(src_root: Path) -> None:
    """Every ``run_tool_json(...)`` call names its ``scope=``."""
    offenders: list[str] = []
    for py in sorted((src_root / "web").rglob("*.py")):
        rel = str(py.relative_to(src_root))
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            )
            if name != "run_tool_json":
                continue
            if not any(kw.arg == "scope" for kw in node.keywords):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, "run_tool_json() without scope=:\n" + "\n".join(offenders)


def _executor_parsers() -> dict[str, argparse.ArgumentParser]:
    """Build the argparse parsers of every executor CLI that may run in docker."""
    from ovid2.cli import analysis, dq_checks, etl, health
    from ovid2.tools import copy, move

    parsers: dict[str, argparse.ArgumentParser] = {}
    for name, module in (
        ("etl", etl),
        ("analysis", analysis),
        ("health", health),
        ("dq_checks", dq_checks),
        ("copy", copy),
        ("move", move),
    ):
        root = argparse.ArgumentParser()
        sub = root.add_subparsers(dest="command")
        before = set(sub.choices)
        module.register(sub)
        for choice in set(sub.choices) - before:
            parsers[f"{name}:{choice}"] = sub.choices[choice]
    return parsers


@pytest.mark.contract
def test_docker_secret_scope_covers_every_address_and_connection_arg() -> None:
    """``secret_scope_for_args`` knows every ``*_address`` / ``*_connection`` dest."""
    from ovid2.core import docker

    known_addresses = set(docker.SCOPED_ADDRESS_ARGS)
    known_connections = set(docker.SCOPED_CONNECTION_ARGS)
    missing: list[str] = []
    for label, parser in _executor_parsers().items():
        for action in parser._actions:
            dest = action.dest or ""
            if dest.endswith("_address") and dest not in known_addresses:
                missing.append(f"{label} --{dest} (address)")
            if dest.endswith("_connection") and dest not in known_connections:
                missing.append(f"{label} --{dest} (connection)")
    assert not missing, (
        "Executor CLI args not covered by ovid2.core.docker secret scoping — "
        "add them to SCOPED_ADDRESS_ARGS / SCOPED_CONNECTION_ARGS:\n" + "\n".join(missing)
    )


@pytest.mark.contract
def test_worker_job_scope_covers_job_resources() -> None:
    """A job's scope names its addresses, prev address, connection, engine, pipeline."""
    from ovid2.web.models import JobDetail
    from ovid2.web.worker import job_secret_scope

    job = JobDetail(
        job_id="j",
        submitted_at="2026-01-01T00:00:00Z",
        submitted_by="t",
        pipeline="demo",
        batch_id="b",
        input_address="in_a",
        output_address="out_a",
        tables=[],
        transformations=[],
        extra_args={
            "prev_input_address": "prev_a",
            "connection": "conn_x",
            "engine": "sp",
        },
        status="queued",
    )
    scope = job_secret_scope(job)
    assert set(scope.addresses) == {"in_a", "out_a", "prev_a"}
    assert scope.connections == ("conn_x",)
    assert scope.engines == ("sp",)
    assert scope.pipelines == ("demo",)
