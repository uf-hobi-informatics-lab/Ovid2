"""Contract: the Ovid Atlas (``/navigator``) invariants (adr-atlas.md).

* The node-kind catalogue of the server (``atlas_svc.NODE_KINDS``) and the
  browser's renderer table (``static/atlas/kinds.js`` ``KINDS``) agree, kind
  for kind and layer for layer — a new node kind needs both.
* The three layers are exactly storage, compute and algorithm.
* The Atlas services are web-tier modules: they never import the executor
  (``ovid2.core`` / engines / drivers' data planes) at module level.
* The Atlas API adds no second write path: its only mutating routes are the
  shared-view save / delete, the presence heartbeat and the rebind dry-run.
  Run / pause / cancel / save-spec go through the routes that own them.
* Every table loop that ``table_runner`` / copy / move / the legacy runner
  / the health runner owns calls the operator-pause gate
  (:func:`ovid2.core.pause.gate`) between tables — so a pause never
  interrupts a running table.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src" / "ovid2"
_KINDS_JS = _SRC / "web" / "static" / "atlas" / "kinds.js"

pytestmark = pytest.mark.contract


def _js_kinds() -> dict[str, str]:
    text = _KINDS_JS.read_text(encoding="utf-8")
    block = text[text.index("export const KINDS = {") :]
    block = block[: block.index("};")]
    return dict(re.findall(r'^\s*(\w+):\s*\{\s*layer:\s*"(\w+)"', block, re.M))


def test_node_kinds_match_renderer_catalogue() -> None:
    from ovid2.web.services.atlas_svc import NODE_KINDS

    assert _js_kinds() == NODE_KINDS


def test_layers_are_the_three_orthogonal_planes() -> None:
    from ovid2.web.services.atlas_svc import LAYERS, NODE_KINDS

    assert set(LAYERS) == {"storage", "compute", "algorithm"}
    assert set(NODE_KINDS.values()) == set(LAYERS)


@pytest.mark.parametrize("module", ["atlas_svc.py", "atlas_state_svc.py"])
def test_atlas_services_do_not_import_the_executor(module: str) -> None:
    tree = ast.parse((_SRC / "web" / "services" / module).read_text(encoding="utf-8"))
    forbidden = ("ovid2.core", "ovid2.transformations", "ovid2.formatters", "pyspark", "duckdb")
    for node in tree.body:  # module level only; lazy imports are reviewed separately
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for n in names:
            assert not n.startswith(forbidden), f"{module} imports {n} at module level"
    text = (_SRC / "web" / "services" / module).read_text(encoding="utf-8")
    assert "ovid2.core" not in re.sub(r'"""[\s\S]*?"""|#.*', "", text)


_ALLOWED_ATLAS_WRITES = {
    ("PUT", "/api/v1/atlas/layouts/{name}"),
    ("DELETE", "/api/v1/atlas/layouts/{name}"),
    ("POST", "/api/v1/atlas/presence"),
    ("POST", "/api/v1/atlas/rebind:check"),
}


def test_atlas_api_adds_no_second_write_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from ovid2.web.app import create_app

    from ._routes import iter_api_routes

    writes = {
        (m, r.path)
        for r, _ in iter_api_routes(create_app())
        if r.path.startswith("/api/v1/atlas")
        for m in r.methods
        if m in {"POST", "PUT", "PATCH", "DELETE"}
    }
    assert writes == _ALLOWED_ATLAS_WRITES


#: Files whose table loop must hold at the pause gate, and the call that does.
_PAUSE_GATED = {
    "core/table_runner.py": "before_table()",
    "cli/etl.py": "_pause_gate(",
    "tools/copy.py": "hold()",
    "tools/move.py": "hold()",
    "legacy/runner.py": "hold()",
    "transformations/quality/runner.py": "hold()",
}


@pytest.mark.parametrize(("rel", "call"), sorted(_PAUSE_GATED.items()))
def test_table_loops_hold_at_the_pause_gate(rel: str, call: str) -> None:
    text = (_SRC / rel).read_text(encoding="utf-8")
    assert call in text, f"{rel}: the table loop must call {call} (operator pause)"


def test_pause_flag_is_shared_web_safe_code() -> None:
    """The worker and the executor agree on one flag path (config.run_control)."""
    core = (_SRC / "core" / "pause.py").read_text(encoding="utf-8")
    worker = (_SRC / "web" / "worker.py").read_text(encoding="utf-8")
    assert "from ovid2.config.run_control import is_paused" in core
    assert "run_control.request_pause" in worker and "run_control.clear_pause" in worker


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
