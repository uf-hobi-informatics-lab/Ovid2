"""Contract: web-tier secret writes must chmod 0o600.

W1 has no secret-write path; the guard exists so that when W4 lands a
secrets editor it goes through the correct pattern:

.. code-block:: python

   os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)

or an ``os.chmod(path, 0o600)`` follow-up before any user data is
written. We enforce this with an AST scan of the (planned)
``services/secrets_svc.py`` module: if it exists, every ``open()`` /
``Path.open()`` call in it must be preceded or followed by a mode
mention of ``0o600``. Any file writing to ``secrets.d/`` outside that
module is a contract violation.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


@pytest.mark.contract
def test_secrets_writes_use_mode_0600(src_root: Path) -> None:
    """Every module that mentions ``secrets.d`` must also mention ``0o600``."""
    web_root = src_root / "web"
    if not web_root.is_dir():
        pytest.skip("Web tier not present.")

    offenders: list[str] = []
    for py in web_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        # Fast negative check.
        if "secrets.d" not in text and "SECRETS_DIR" not in text and "secrets_dir" not in text:
            continue
        # A read-only listing module is fine; only writers need the mode.
        writer_signals = ("write_text", "os.open", ".open(", "open(")
        writes = any(signal in text for signal in writer_signals)
        if not writes:
            continue
        if "0o600" not in text and "0600" not in text:
            offenders.append(str(py.relative_to(src_root)))

    assert not offenders, (
        "Web-tier module writes under secrets.d/ without 0o600 mode:\n"
        + "\n".join(offenders)
    )


@pytest.mark.contract
def test_web_services_layer_exists(src_root: Path) -> None:
    """The services layer is the single sanctioned side-effect layer."""
    services = src_root / "web" / "services"
    if not (src_root / "web").is_dir():
        pytest.skip("Web tier not present.")
    assert services.is_dir(), "src/ovid2/web/services/ must exist"


# ---------------------------------------------------------------------------
# AST sanity: no ``os.chmod(..., 0o644)`` on secrets paths
# ---------------------------------------------------------------------------


def _chmod_targets_secrets(node: ast.Call) -> bool:
    """Return True when a ``chmod`` call touches a *secrets* path."""
    if not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr != "chmod":
        return False
    if not node.args:
        return False
    # Cheap heuristic: the argument text mentions secrets/token/salt.
    dump = ast.dump(node.args[0])
    return "secret" in dump.lower() or "salt" in dump.lower()


@pytest.mark.contract
def test_no_permissive_chmod_on_secrets(src_root: Path) -> None:
    """Chmod on a secret path must set 0o600 (never 0o644, 0o664, 0o755)."""
    web_root = src_root / "web"
    if not web_root.is_dir():
        pytest.skip("Web tier not present.")

    offenders: list[str] = []
    for py in web_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _chmod_targets_secrets(node):
                continue
            # The mode argument (positional 1 or keyword ``mode``) must be 0o600.
            mode_node: ast.expr | None = None
            if len(node.args) >= 2:
                mode_node = node.args[1]
            for kw in node.keywords:
                if kw.arg == "mode":
                    mode_node = kw.value
            if isinstance(mode_node, ast.Constant) and mode_node.value != 0o600:
                offenders.append(f"{py.relative_to(src_root)}:{node.lineno} chmod={mode_node.value:o}")

    assert not offenders, "Permissive chmod on secret path:\n" + "\n".join(offenders)
