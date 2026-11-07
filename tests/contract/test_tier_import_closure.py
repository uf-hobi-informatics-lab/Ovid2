"""Contract: the control-plane tiers have a clean *transitive* import closure.

``test_web_no_pipeline_imports.py`` checks the import statements written in
``src/ovid2/web/**``. It cannot see modules pulled in indirectly — e.g.
``ovid2.config.addresses`` importing ``ovid2.core.addresses.base`` at
module level, or ``ovid2.scheduler.__init__`` eagerly loading
``scheduler.service`` (→ ``ovid2.core.events`` / ``core.isolation``).

This test imports every module of a tier in a **fresh interpreter** and
asserts that none of the data-plane / pipeline packages, engines, or heavy
storage SDKs ended up in ``sys.modules``. Imports done lazily inside a
function body are deliberately allowed: they run in the executor
subprocess, never at web / MCP start-up.

See the adr-harness-api-executor-trust-model design note
("Control plane vs data plane").
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

#: Packages the web tier must never load at import time.
WEB_FORBIDDEN: tuple[str, ...] = (
    "ovid2.core",
    "ovid2.transformations",
    "ovid2.formatters",
    "ovid2.mappers",
    "ovid2.semantic",
    "ovid2.llm",
    "ovid2.tools.table_ops",
    "ovid2.exports",
    "pyspark",
    "duckdb",
    "snowflake.snowpark",
    "snowflake.connector",
    "boto3",
    "botocore",
    "azure.storage",
    "azure.identity",
    "google.cloud",
    "psycopg",
    "psycopg2",
    "pyodbc",
    "pymssql",
    "databricks",
)

#: The only ``ovid2.*`` packages the MCP (agent) tier may load.
MCP_ALLOWED_OVID2: tuple[str, ...] = (
    "ovid2.mcp",
    "ovid2.web.settings",
    "ovid2.config.paths",
)

_PROBE = r"""
import importlib, json, pkgutil, sys
root = sys.argv[1]
pkg = importlib.import_module(root)
failures = []
for info in pkgutil.walk_packages(pkg.__path__, root + "."):
    try:
        importlib.import_module(info.name)
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        failures.append(f"{info.name}: {type(exc).__name__}: {exc}")
print(json.dumps({"modules": sorted(sys.modules), "failures": failures}))
"""


def _closure(root: str) -> tuple[list[str], list[str]]:
    """Import every module under ``root`` in a fresh interpreter."""
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE, root],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    return payload["modules"], payload["failures"]


def _matches(name: str, roots: tuple[str, ...]) -> bool:
    return any(name == r or name.startswith(r + ".") for r in roots)


@pytest.mark.contract
def test_web_tier_import_closure_is_control_plane_only() -> None:
    """Importing all of ``ovid2.web`` loads no pipeline / engine / SDK module."""
    pytest.importorskip("fastapi")
    modules, failures = _closure("ovid2.web")
    assert not failures, "web modules failed to import:\n" + "\n".join(failures)
    offenders = [m for m in modules if _matches(m, WEB_FORBIDDEN)]
    assert not offenders, (
        "The web tier transitively imports data-plane modules. Move the "
        "import inside the function that needs it, or under TYPE_CHECKING:\n" + "\n".join(offenders)
    )


@pytest.mark.contract
def test_mcp_tier_import_closure_is_capability_only() -> None:
    """Importing all of ``ovid2.mcp`` loads only the allowed ``ovid2`` modules."""
    pytest.importorskip("mcp")
    modules, failures = _closure("ovid2.mcp")
    assert not failures, "mcp modules failed to import:\n" + "\n".join(failures)
    offenders = [
        m
        for m in modules
        if m.startswith("ovid2.")
        and m not in {"ovid2.config", "ovid2.web"}  # namespace parents
        and not _matches(m, MCP_ALLOWED_OVID2)
    ]
    offenders += [m for m in modules if _matches(m, WEB_FORBIDDEN)]
    assert not offenders, "The MCP tier transitively imports:\n" + "\n".join(offenders)
