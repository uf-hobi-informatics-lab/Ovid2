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


#: Files allowed to call ``importlib.import_module`` with a non-literal
#: argument, and why. The tool loader only ever imports its own
#: ``ovid2.mcp.tools.<name>`` siblings (``pkgutil.iter_modules``).
_DYNAMIC_IMPORT_OK: frozenset[str] = frozenset({"mcp/tools/__init__.py"})


#: Symbols the agent tier may take from the top-level ``ovid2`` package.
_ALLOWED_ROOT_SYMBOLS: frozenset[str] = frozenset({"ovid2.__version__"})


def _is_forbidden(target: str) -> bool:
    """Return True if ``target`` resolves into any forbidden root."""
    return any(target == root or target.startswith(root + ".") for root in FORBIDDEN_ROOTS)


def _mcp_imports(src_root: Path) -> list[tuple[str, int, str]]:
    """Return ``(file, line, module)`` for every static and literal dynamic import."""
    from tests.contract._ast_utils import dynamic_imports, iter_imports

    out: list[tuple[str, int, str]] = []
    for py in (src_root / "mcp").rglob("*.py"):
        rel = py.relative_to(src_root).as_posix()
        package = "ovid2." + ".".join(py.relative_to(src_root).with_suffix("").parts[:-1])
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for ref in iter_imports(tree, package):
            out.append((rel, ref.lineno, ref.module))
        for dyn in dynamic_imports(tree):
            out.append((rel, dyn.lineno, dyn.module or "<non-literal>"))
    return out


@pytest.mark.contract
def test_mcp_does_not_import_secrets_storage_or_pipeline(src_root: Path) -> None:
    """No MCP module may import secret, driver/connection, or pipeline code.

    Covers ``import a, b``, ``from ovid2 import core`` (→ ``ovid2.core``),
    relative imports, and literal ``importlib.import_module("…")`` /
    ``__import__("…")`` calls; a non-literal dynamic import is refused
    outside the tool loader.
    """
    if not (src_root / "mcp").is_dir():
        pytest.skip("MCP tier not present.")
    offenders: list[str] = []
    for rel, line, module in _mcp_imports(src_root):
        if module == "<non-literal>":
            if rel not in _DYNAMIC_IMPORT_OK:
                offenders.append(f"{rel}:{line} non-literal dynamic import")
            continue
        if _is_forbidden(module):
            offenders.append(f"{rel}:{line} imports {module}")
    assert not offenders, (
        "Agent tier (MCP) must not import secret / storage / pipeline "
        "packages (see "
        ".opencode/knowledge/delta/adr-harness-api-executor-trust-model.md):\n"
        + "\n".join(offenders)
    )


@pytest.mark.contract
def test_mcp_only_imports_allowlisted_ovid2_modules(src_root: Path) -> None:
    """Any ``ovid2.*`` import in MCP must be MCP-internal or allow-listed."""
    if not (src_root / "mcp").is_dir():
        pytest.skip("MCP tier not present.")
    offenders = [
        f"{rel}:{line} imports {module}"
        for rel, line, module in _mcp_imports(src_root)
        if (module == "ovid2" or module.startswith("ovid2."))
        and not module.startswith("ovid2.mcp")
        and module not in ALLOWED_EXACT
        # ``from <allowed> import NAME`` also yields ``<allowed>.NAME`` (it
        # might be a submodule); a symbol of an allowed module is fine.
        and not (
            module.rpartition(".")[0] in ALLOWED_EXACT - {"ovid2"}
            or module in _ALLOWED_ROOT_SYMBOLS
        )
    ]
    assert not offenders, (
        "Agent tier (MCP) imported a non-allow-listed ovid2 module. "
        "Allowed: ovid2.mcp.*, ovid2.web.settings, ovid2 (__version__).\n" + "\n".join(offenders)
    )


@pytest.mark.contract
def test_detector_catches_evasions(tmp_path: Path) -> None:
    """Negative control: the evasion spellings are all reported."""
    mcp = tmp_path / "mcp"
    mcp.mkdir()
    (mcp / "bad.py").write_text(
        "import json, ovid2.core\n"
        "from ovid2 import drivers\n"
        "import importlib\n"
        "importlib.import_module('ovid2.config.secrets')\n"
        "importlib.import_module(name)\n",
        encoding="utf-8",
    )
    found = {m for _r, _l, m in _mcp_imports(tmp_path)}
    assert {"ovid2.core", "ovid2.drivers", "ovid2.config.secrets", "<non-literal>"} <= found
    assert all(_is_forbidden(m) for m in ("ovid2.core", "ovid2.drivers", "ovid2.config.secrets"))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
