"""Contract: :mod:`ovid2.packs` is tier-neutral.

The web tier, the manual generator and the executor all read the pack
catalogue through :mod:`ovid2.packs`, so importing it (and walking every
pack) must never load pipeline code, an engine, a heavy SDK — or any
installed pack's own Python code. Pack *code* (``operators_module`` /
``fixers_module``) is imported only by the executor registries
(``ovid2.transformations.quality.ops`` and
``ovid2.transformations.fixers.common.registry``).

ADR: the adr-domain-packs design note.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.contract.test_tier_import_closure import WEB_FORBIDDEN

#: ``ovid2.*`` modules the packs package may load at import / discovery time.
_ALLOWED_OVID2: tuple[str, ...] = (
    "ovid2",
    "ovid2.packs",
    "ovid2.config",
    "ovid2.config.paths",
    "ovid2.resources",
    "ovid2.resources.schemas",
    "ovid2.resources.packs",
)

#: Modules that may import pack code (executor tier only).
_PACK_CODE_IMPORTERS: frozenset[str] = frozenset(
    {
        "src/ovid2/transformations/quality/ops/__init__.py",
        "src/ovid2/transformations/fixers/common/registry.py",
    }
)

_PROBE = r"""
import json, sys
import ovid2.packs as packs
from ovid2.packs import content, repair
infos = packs.all_packs()
packs.active_packs()
content.iter_check_docs()
content.iter_package_docs()
content.part_order()
print(json.dumps({"modules": sorted(sys.modules), "packs": [p.id for p in infos]}))
"""


def _matches(name: str, roots: tuple[str, ...]) -> bool:
    return any(name == r or name.startswith(r + ".") for r in roots)


@pytest.mark.contract
def test_packs_import_closure_is_tier_neutral() -> None:
    """Discovery + content iteration load no pipeline / engine / SDK module."""
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    modules: list[str] = payload["modules"]
    assert "pcornet_7_0" in payload["packs"]
    offenders = [m for m in modules if _matches(m, WEB_FORBIDDEN)]
    offenders += [
        m
        for m in modules
        if m.startswith("ovid2.") and m not in _ALLOWED_OVID2 and not _matches(m, ("ovid2.packs",))
    ]
    assert not offenders, "ovid2.packs transitively imports:\n" + "\n".join(offenders)


@pytest.mark.contract
def test_only_executor_registries_import_pack_code(repo_root: Path) -> None:
    """``importlib.import_module`` on pack modules lives only in the executor registries.

    A pack's ``operators_module`` / ``fixers_module`` is third-party code.
    Any other module reading ``provides.operators_module`` /
    ``provides.fixers_module`` and importing it would leak pack code into
    the web / MCP tier.
    """
    offenders: list[str] = []
    for path in sorted((repo_root / "src" / "ovid2").rglob("*.py")):
        rel = path.relative_to(repo_root).as_posix()
        text = path.read_text(encoding="utf-8")
        if "operators_module" not in text and "fixers_module" not in text:
            continue
        tree = ast.parse(text)
        imports_dynamically = any(
            isinstance(node, ast.Attribute) and node.attr == "import_module"
            for node in ast.walk(tree)
        )
        if imports_dynamically and rel not in _PACK_CODE_IMPORTERS:
            offenders.append(rel)
    assert not offenders, (
        "Only the executor registries may import a pack's operators_module / "
        "fixers_module:\n" + "\n".join(offenders)
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
