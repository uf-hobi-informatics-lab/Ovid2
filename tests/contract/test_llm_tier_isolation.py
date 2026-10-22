"""Contract: the LLM runtime (``ovid2.llm``) is executor-tier only (L1).

``ovid2.llm`` resolves LLM API keys. Per the harness / API / executor
trust model the web tier (``ovid2.web``) and the agent tier
(``ovid2.mcp``) must never import it — the web tier reaches it only via
the ``python -m ovid2.llm`` subprocess launched in
``web/services/llm_exec.py``. Also pins the two hand-maintained
provider catalogues together and keeps heavy SDKs out of ``ovid2.llm``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_HEAVY = ("pyspark", "snowflake", "boto3", "azure", "google.cloud", "databricks")


def _imports(py: Path) -> list[tuple[int, str]]:
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((node.lineno, a.name) for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append((node.lineno, node.module))
    return out


@pytest.mark.contract
@pytest.mark.parametrize("tier", ["web", "mcp"])
def test_tier_does_not_import_llm_runtime(src_root: Path, tier: str) -> None:
    """``ovid2.web`` / ``ovid2.mcp`` never import ``ovid2.llm``."""
    offenders = [
        f"{py.relative_to(src_root)}:{line} imports {mod}"
        for py in (src_root / tier).rglob("*.py")
        for line, mod in _imports(py)
        if mod == "ovid2.llm" or mod.startswith("ovid2.llm.")
    ]
    assert not offenders, "LLM runtime is executor-only:\n" + "\n".join(offenders)


@pytest.mark.contract
def test_llm_runtime_has_no_heavy_imports(src_root: Path) -> None:
    """``ovid2.llm`` stays install-light (httpx only)."""
    offenders = [
        f"{py.relative_to(src_root)}:{line} imports {mod}"
        for py in (src_root / "llm").rglob("*.py")
        for line, mod in _imports(py)
        if any(mod == h or mod.startswith(h + ".") for h in _HEAVY)
    ]
    assert not offenders, "\n".join(offenders)


@pytest.mark.contract
def test_provider_catalogues_synchronised() -> None:
    """``config.llms`` (web-safe) and ``llm.providers`` agree on every provider."""
    from ovid2.config import llms as cfg
    from ovid2.llm.providers import PROVIDER_TYPE_MAP, load_provider_class

    assert set(cfg.list_providers()) == set(PROVIDER_TYPE_MAP)
    for name in PROVIDER_TYPE_MAP:
        cls = load_provider_class(name)
        assert cls.provider_name == name
        assert set(cls.roles) == set(cfg.provider_roles(name)), name
        spec = cfg.llm_field_spec(name)
        arg_names = {f["name"] for f in spec["args"]}
        secret_names = {f["name"] for f in spec["secrets"]}
        assert not (arg_names & secret_names), name
        assert all(r in cfg.ROLES for r in spec["roles"]), name
