"""Contract: destructive delete endpoints pass through the approval gate.

In the harness / API / executor trust model
(the adr-harness-api-executor-trust-model design note)
an untrusted agent may propose destructive operations, but a human must
approve them first. This test guards that every ``DELETE`` handler under
``web/routes/api_v1.py`` for a gated resource calls ``_gate(...)`` — so a
future delete endpoint cannot silently skip the approval gate.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: Delete handler function names that must invoke the approval gate.
GATED_DELETE_HANDLERS: frozenset[str] = frozenset(
    {
        "api_delete_secret",
        "api_delete_connection",
        "api_delete_engine",
        "api_delete_pipeline_config",
        "api_delete_address",
        "api_delete_llm",
    }
)


def _calls_gate(func: ast.FunctionDef) -> bool:
    """Return True if the function body calls ``_gate(...)``."""
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_gate"
        ):
            return True
    return False


@pytest.mark.contract
def test_destructive_deletes_are_gated(src_root: Path) -> None:
    """Every gated delete handler must call the approval gate."""
    api = src_root / "web" / "routes" / "api_v1.py"
    if not api.is_file():
        pytest.skip("Web API routes not present.")

    tree = ast.parse(api.read_text(encoding="utf-8"), filename=str(api))
    found: dict[str, bool] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in GATED_DELETE_HANDLERS:
            found[node.name] = _calls_gate(node)

    missing_handlers = GATED_DELETE_HANDLERS - set(found)
    assert not missing_handlers, (
        f"Expected gated delete handlers not found in api_v1.py: {sorted(missing_handlers)}"
    )
    ungated = sorted(name for name, gated in found.items() if not gated)
    assert not ungated, (
        "These destructive delete handlers do not call the approval "
        "gate `_gate(...)` (see "
        "adr-harness-api-executor-trust-model.md):\n" + "\n".join(ungated)
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
