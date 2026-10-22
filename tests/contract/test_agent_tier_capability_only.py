"""Contract: the agent tier (MCP) is a capability-only client.

"Agent Ovid" — the LLM-driven harness — must be able to *propose*
actions but never *hold* secrets or touch data storage directly. In
the harness / API / executor trust model
(the adr-harness-api-executor-trust-model design note)
the MCP server is the untrusted planner: it may only speak the typed
capability API over HTTP (via :class:`ovid2.mcp.client.WebApiClient`).

Concretely, no file under ``src/ovid2/mcp/`` may import from:

- ``ovid2.config.secrets`` / ``ovid2.config.connections`` /
  ``ovid2.config.engines`` (secret + credential resolution)
- ``ovid2.drivers`` / ``ovid2.connections`` (storage drivers/connectors)
- ``ovid2.core`` / ``ovid2.transformations`` / ``ovid2.formatters`` /
  ``ovid2.mappers`` / ``ovid2.semantic`` (the pipeline)

Rationale: a prompt-injected agent must not be able to reach a code
path that dereferences a credential or opens a storage handle. The
only sanctioned side-effect channel is an HTTP request to the ovid2
API, which authenticates, authorizes, and audits every call.

Approved ``ovid2`` dependencies for the agent tier:

- ``ovid2.mcp.*`` (the tier itself)
- ``ovid2.web.settings`` (upstream URL / bind — no secrets, no I/O)
- ``ovid2`` (package ``__version__`` only)
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

FORBIDDEN_ROOTS: tuple[str, ...] = (
    "ovid2.config.secrets",
    "ovid2.config.connections",
    "ovid2.config.engines",
    "ovid2.drivers",
    "ovid2.connections",
    "ovid2.core",
    "ovid2.transformations",
    "ovid2.formatters",
    "ovid2.mappers",
    "ovid2.semantic",
)

#: Exact ``ovid2`` modules the agent tier may import.
ALLOWED_EXACT: frozenset[str] = frozenset({"ovid2", "ovid2.web.settings"})


def _module_target(node: ast.AST) -> str | None:
    """Return the fully-qualified module referenced by an import node."""
    if isinstance(node, ast.Import):
        return node.names[0].name if node.names else None
    if isinstance(node, ast.ImportFrom):
        return node.module
    return None


def _is_forbidden(target: str) -> bool:
    """Return True if ``target`` resolves into any forbidden root."""
    return any(target == root or target.startswith(root + ".") for root in FORBIDDEN_ROOTS)


@pytest.mark.contract
def test_mcp_does_not_import_secrets_storage_or_pipeline(src_root: Path) -> None:
    """No MCP module may import secret, driver/connection, or pipeline code."""
    mcp_root = src_root / "mcp"
    if not mcp_root.is_dir():
        pytest.skip("MCP tier not present.")

    offenders: list[str] = []
    for py in mcp_root.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            target = _module_target(node)
            if target and _is_forbidden(target):
                offenders.append(
                    f"{py.relative_to(src_root)}:{node.lineno} imports {target}"
                )

    assert not offenders, (
        "Agent tier (MCP) must not import secret / storage / pipeline "
        "packages (see "
        ".opencode/knowledge/delta/adr-harness-api-executor-trust-model.md):\n"
        + "\n".join(offenders)
    )


@pytest.mark.contract
def test_mcp_only_imports_allowlisted_ovid2_modules(src_root: Path) -> None:
    """Any ``ovid2.*`` import in MCP must be MCP-internal or allow-listed."""
    mcp_root = src_root / "mcp"
    if not mcp_root.is_dir():
        pytest.skip("MCP tier not present.")

    offenders: list[str] = []
    for py in mcp_root.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            target = _module_target(node)
            if not target or not (target == "ovid2" or target.startswith("ovid2.")):
                continue
            if target.startswith("ovid2.mcp"):
                continue
            if target in ALLOWED_EXACT:
                continue
            offenders.append(
                f"{py.relative_to(src_root)}:{node.lineno} imports {target}"
            )

    assert not offenders, (
        "Agent tier (MCP) imported a non-allow-listed ovid2 module. "
        "Allowed: ovid2.mcp.*, ovid2.web.settings, ovid2 (__version__).\n"
        + "\n".join(offenders)
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
