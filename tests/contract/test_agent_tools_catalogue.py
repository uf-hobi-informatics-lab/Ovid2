"""Contract: the assistant's agent-tool catalogue is safe and in sync (L2).

The catalogue (``resources/agent_tools/catalogue.yaml``) is the allow-list
of ``/api/v1`` operations the right-panel assistant may call. These
checks keep it honest:

1. It loads with the real loader (schema + placeholder validation).
2. Every entry names a real route + method in the pinned OpenAPI
   snapshot, and the capability it declares equals the capability the
   route actually enforces.
3. ``mutating`` ⇔ non-GET, and no DELETE is ever listed.
4. Sensitive surfaces are never exposed (secrets, settings writes,
   install, users, approvals, LLM connections, storage browse, file
   preview, filesystem listing, assistant chat itself).
5. The executor-side runtime can only ever issue GET.
6. UI actions (``ui_action: true``) have no route at all, are limited to
   a pinned set of cosmetic browser actions, and their argument
   allow-lists agree with the browser's (theme.js /
   _appearance_prepaint.html / appearance.js / llm_panel.js).
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest

from ovid2.agent_tools import load_catalogue

_SNAPSHOT = Path(__file__).parent / "data" / "openapi.snapshot.json"

_FORBIDDEN_PREFIXES = (
    "/api/v1/secrets",
    "/api/v1/settings",
    "/api/v1/install",
    "/api/v1/users",
    "/api/v1/approvals",
    "/api/v1/llms",
    "/api/v1/llm/",
    "/api/v1/storage",
    "/api/v1/fs",
    # Data Space table *rows* (patient data) never reach the LLM; only
    # the metadata-only ``GET /api/v1/dataspace/table`` is allowed.
    "/api/v1/dataspace/table/",
)
_FORBIDDEN_SUFFIXES = ("/file", "/events.log", "/metrics/stream", "/artifact")


@pytest.mark.contract
def test_catalogue_loads() -> None:
    tools = load_catalogue()
    assert tools, "catalogue must not be empty"
    assert len({t.name for t in tools}) == len(tools)


@pytest.mark.contract
def test_every_tool_matches_a_real_route() -> None:
    spec = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    paths = spec["paths"]
    missing = [
        f"{t.method} {t.path} ({t.name})"
        for t in load_catalogue()
        if not t.ui_action and t.method.lower() not in paths.get(t.path, {})
    ]
    assert not missing, "catalogue references unknown routes:\n" + "\n".join(missing)


def _flat_routes(app: Any) -> list[Any]:
    """Every concrete route, across FastAPI versions.

    FastAPI >= 0.12x keeps ``include_router`` children behind lazy
    ``_IncludedRouter`` entries in ``app.routes`` (and exposes
    ``fastapi.routing.iter_route_contexts`` to flatten them); older
    versions expand them in place.
    """
    from fastapi import routing

    iter_contexts = getattr(routing, "iter_route_contexts", None)
    if iter_contexts is None:
        return list(app.routes)
    return [ctx.route for ctx in iter_contexts(app.routes)]


@pytest.mark.contract
def test_declared_capability_matches_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from ovid2.web.app import create_app

    enforced: dict[tuple[str, str], set[str]] = {}
    for route in _flat_routes(create_app()):
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        caps: set[str] = set()
        for dep in dependant.dependencies:
            for cell in getattr(dep.call, "__closure__", None) or ():
                value = cell.cell_contents
                if type(value).__name__ == "Capability":
                    caps.add(value.value)
        for method in getattr(route, "methods", set()):
            enforced[(method, route.path)] = caps
    # Guard against a vacuous pass if route discovery ever breaks again.
    assert sum(1 for caps in enforced.values() if caps) > 10, "no capability-gated routes found"
    bad = [
        f"{t.name}: declares {t.capability}, route enforces "
        f"{sorted(enforced.get((t.method, t.path), set()))}"
        for t in load_catalogue()
        if not t.ui_action and enforced.get((t.method, t.path)) != {t.capability}
    ]
    assert not bad, "\n".join(bad)


@pytest.mark.contract
def test_mutating_flag_and_no_delete() -> None:
    for tool in load_catalogue():
        if tool.ui_action:
            assert not tool.mutating and not tool.method and not tool.path, tool.name
            assert tool.description.startswith("UI:"), (
                f"{tool.name}: ui_action tools must start their description with UI:"
            )
            continue
        assert tool.method in ("GET", "POST", "PUT"), tool.name
        assert tool.mutating is (tool.method != "GET"), tool.name
        assert not tool.description.startswith("UI:"), tool.name
        if tool.opens_draft:
            assert tool.description.startswith("DRAFT"), (
                f"{tool.name}: opens_draft tools must start their description with DRAFT"
            )
        elif tool.mutating:
            assert tool.description.startswith("PROPOSE"), (
                f"{tool.name}: write tools must start their description with PROPOSE "
                "so the model knows they only create a confirmation card"
            )


# Routes that create a *non-executing* draft job. ``opens_draft`` (browser
# runs the call without a proposal click) is allowed ONLY on these — the
# human gate is the ▶ on the draft's run page. Widening this list is an
# ADR change (adr-llm-dual-system.md, "Draft jobs").
_DRAFT_ROUTES = {("POST", "/api/v1/job-drafts"), ("POST", "/api/v1/pipelines/{name}/draft")}


@pytest.mark.contract
def test_opens_draft_only_on_draft_routes() -> None:
    bad = [
        f"{t.name} → {t.method} {t.path}"
        for t in load_catalogue()
        if t.opens_draft and (t.method, t.path) not in _DRAFT_ROUTES
    ]
    assert not bad, "opens_draft is reserved for draft-creating routes:\n" + "\n".join(bad)


@pytest.mark.contract
def test_draft_routes_never_queue() -> None:
    """Draft routes store ``status='draft'``, which the worker never claims."""
    src = (
        Path(__file__).resolve().parents[2] / "src" / "ovid2" / "web" / "routes" / "api_v1.py"
    ).read_text(encoding="utf-8")

    def _body(fn: str) -> str:
        m = re.search(rf"def {fn}\(.*?\n(?=@router|\Z)", src, re.S)
        assert m, f"{fn} route missing"
        return m.group(0)

    a = _body("api_create_job_draft")
    assert "jobs_svc.create_draft(" in a and "jobs_svc.submit(" not in a
    b = _body("api_draft_pipeline_config")
    assert "draft=True" in b and "jobs_svc.submit(" not in b


@pytest.mark.contract
def test_no_sensitive_surfaces() -> None:
    bad = [
        f"{t.name} → {t.path}"
        for t in load_catalogue()
        if t.path.startswith(_FORBIDDEN_PREFIXES) or t.path.endswith(_FORBIDDEN_SUFFIXES)
    ]
    assert not bad, "sensitive routes must not be agent tools:\n" + "\n".join(bad)


@pytest.mark.contract
def test_executor_never_issues_non_get(src_root: Path) -> None:
    """``ovid2.llm`` never calls an HTTP mutation on the Ovid2 API client."""
    py = src_root / "llm" / "api_client.py"
    tree = ast.parse(py.read_text(encoding="utf-8"))
    verbs = {"post", "put", "patch", "delete"}
    calls = [
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in verbs
    ]
    assert not calls, f"api_client must be GET-only, found {calls}"
    loop = (src_root / "llm" / "tool_loop.py").read_text(encoding="utf-8")
    assert re.search(r"client\.request\(\s*\"GET\"", loop), "tool_loop must request GET only"


@pytest.mark.contract
def test_agent_tools_package_is_tier_neutral(src_root: Path) -> None:
    """``ovid2.agent_tools`` imports nothing from web / mcp / llm / executor tiers."""
    banned = (
        "ovid2.web",
        "ovid2.mcp",
        "ovid2.llm",
        "ovid2.core",
        "ovid2.drivers",
        "ovid2.connections",
        "ovid2.config.secrets",
    )
    offenders = []
    for py in (src_root / "agent_tools").rglob("*.py"):
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            mods = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module]
                if isinstance(node, ast.ImportFrom) and node.module
                else []
            )
            offenders += [
                f"{py.name}: {m}"
                for m in mods
                if any(m == b or m.startswith(b + ".") for b in banned)
            ]
    assert not offenders, "\n".join(offenders)


# ---------------------------------------------------------------------------
# UI actions (browser-only, cosmetic). Widening this set is an ADR change
# (adr-llm-dual-system.md, "UI actions"): each new name needs a matching,
# re-validating handler in llm_panel.js and must stay cosmetic + undoable.
# ---------------------------------------------------------------------------

_UI_ACTIONS = {"set_appearance", "save_appearance", "apply_saved_appearance"}
_STATIC = Path(__file__).resolve().parents[2] / "src" / "ovid2" / "web" / "static"
_TEMPLATES = _STATIC.parent / "templates"


def _js_list(src: str, var: str) -> list[str]:
    m = re.search(rf"var {var} = \[(.*?)\];", src, re.S)
    assert m, f"{var} not found"
    return re.findall(r'"([^"]+)"', re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S))


@pytest.mark.contract
def test_ui_actions_are_pinned() -> None:
    names = {t.name for t in load_catalogue() if t.ui_action}
    assert names == _UI_ACTIONS, f"UI actions changed: {sorted(names)}"
    for t in load_catalogue():
        if t.ui_action:
            assert t.params.get("additionalProperties") is False, t.name
            assert t.capability == "use_llm", t.name


@pytest.mark.contract
def test_ui_actions_handled_and_revalidated_in_browser() -> None:
    js = (_STATIC / "llm_panel.js").read_text(encoding="utf-8")
    assert sorted(_js_list(js, "UI_ACTIONS")) == sorted(_UI_ACTIONS)
    assert 'case "ui_action":' in js
    # The browser re-validates before applying, and never fetches for a UI action.
    body = js[js.index("function applyUiAction") : js.index("function undoUiAction")]
    assert "checkUiArgs(" in body and "fetch(" not in body
    assert "isKnownSlug" in js and "ALLOWED_VARS" in js


@pytest.mark.contract
def test_set_appearance_theme_enum_matches_browser() -> None:
    from ovid2.agent_tools import get_tool

    tool = get_tool("set_appearance")
    assert tool is not None
    enum = tool.params["properties"]["theme"]["enum"]
    theme_js = (_STATIC / "theme.js").read_text(encoding="utf-8")
    js_slugs = re.findall(r'\{ slug: "([a-z0-9-]+)"', theme_js)
    assert enum == js_slugs, "set_appearance theme enum must equal THEMES in theme.js"
    prepaint = (_TEMPLATES / "_appearance_prepaint.html").read_text(encoding="utf-8")
    known = _js_list(prepaint, "KNOWN")
    assert set(enum) <= set(known), "pre-paint KNOWN must list every theme"


@pytest.mark.contract
def test_set_appearance_vars_match_browser_allow_list() -> None:
    from ovid2.agent_tools import get_tool

    tool = get_tool("set_appearance")
    assert tool is not None
    model_vars = set(tool.params["properties"]["appearance"]["properties"])
    allowed = set(_js_list((_STATIC / "appearance.js").read_text(encoding="utf-8"), "ALLOWED_VARS"))
    derived = {"--accent-hover", "--accent-subtle", "--accent-fg"}
    assert model_vars == allowed - derived, (
        f"catalogue vs ALLOWED_VARS drift: {sorted(model_vars ^ (allowed - derived))}"
    )
