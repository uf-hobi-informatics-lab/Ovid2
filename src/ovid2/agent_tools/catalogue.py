"""Agent tool catalogue loader (web-safe, executor-safe).

One YAML file — ``resources/agent_tools/catalogue.yaml`` — lists every
``/api/v1`` operation the in-panel assistant may call. Both tiers read
it:

- the **web tier** (``web/services/llm_chat_svc``) to tell the executor
  which tools exist and to list them at ``GET /api/v1/llm/tools``;
- the **LLM executor** (``ovid2.llm.tool_loop``) to build the
  function-calling ``tools=[...]`` array and to map a model tool call
  onto an HTTP request.

This module imports only the stdlib, ``yaml`` and ``jsonschema`` so it
sits below both tiers without violating ``test_llm_tier_isolation.py``
or ``test_agent_tier_capability_only.py``.

Trust rule baked into the shape: ``method == "GET"`` → the executor may
call it with its GET-only per-chat token. Any other method is a
*proposal* — it is surfaced to the human and executed only by their
browser session. The one exception is ``opens_draft: true`` (draft-job
creation only): the browser executes it without a click because a draft
never runs; the human gate is the ▶ on the draft's run page.

A third kind, ``ui_action: true``, has **no** method or path: it never
reaches the API. The executor emits a ``ui_action`` event and the user's
browser re-validates the arguments against its own allow-list, applies a
cosmetic, reversible change to *that* browser (e.g. the page theme) and
offers Undo. Descriptions start with ``UI:`` (contract-enforced).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any
from urllib.parse import quote

import yaml

__all__ = [
    "AgentTool",
    "CatalogueError",
    "ToolCallError",
    "build_request",
    "get_tool",
    "load_catalogue",
    "openai_tools",
    "validate_args",
]

_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


class CatalogueError(ValueError):
    """Raised when the catalogue YAML is malformed."""


class ToolCallError(ValueError):
    """Raised when a model tool call cannot be mapped onto a request."""


@dataclass(frozen=True)
class AgentTool:
    """One catalogue entry.

    Attributes:
        name: Function-calling name the model sees.
        description: Model-facing description.
        method: ``GET`` / ``POST`` / ``PUT`` (empty for a UI action).
        path: Route template, e.g. ``/api/v1/jobs/{job_id}`` (empty for a
            UI action).
        capability: RBAC capability the route requires.
        params: JSON-schema object for the arguments.
        max_result_chars: Truncation bound for the result fed back to the model.
        truncate: Keep the ``head`` or the ``tail`` of an oversized result.
        opens_draft: Mutating tool that creates a non-executing *draft*
            job. The browser runs it without a proposal click and opens
            the draft's run page; the human gate is the ▶ there.
        ui_action: Browser-only cosmetic action (no HTTP call); the
            browser validates, applies and offers Undo.
    """

    name: str
    description: str
    method: str
    path: str
    capability: str
    params: dict[str, Any] = field(default_factory=lambda: {"type": "object",
                                                             "properties": {}})
    max_result_chars: int = 8000
    truncate: str = "head"
    opens_draft: bool = False
    ui_action: bool = False

    @property
    def mutating(self) -> bool:
        """``True`` for every non-GET API tool (→ proposal, never auto-executed).

        UI actions are not mutating: they never touch the API.
        """
        return not self.ui_action and self.method != "GET"

    @property
    def kind(self) -> str:
        """``read`` / ``proposal`` / ``draft`` / ``ui_action``."""
        if self.ui_action:
            return "ui_action"
        if self.opens_draft:
            return "draft"
        return "proposal" if self.mutating else "read"

    @property
    def path_params(self) -> tuple[str, ...]:
        """Placeholder names in :attr:`path`, in order."""
        return tuple(_PLACEHOLDER.findall(self.path))

    def to_openai(self) -> dict[str, Any]:
        """Render as an OpenAI ``tools`` array entry."""
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.params}}

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable public form (for ``GET /api/v1/llm/tools``)."""
        return {"name": self.name, "description": self.description,
                "method": self.method, "path": self.path,
                "capability": self.capability, "mutating": self.mutating,
                "opens_draft": self.opens_draft, "ui_action": self.ui_action,
                "kind": self.kind, "params": self.params}


def _schema() -> dict[str, Any]:
    text = (resources.files("ovid2.resources") / "schemas" / "agent_tools.v1.json").read_text(
        encoding="utf-8")
    loaded: dict[str, Any] = json.loads(text)
    return loaded


def _parse(doc: Any) -> tuple[AgentTool, ...]:
    import jsonschema

    try:
        jsonschema.validate(doc, _schema())
    except jsonschema.ValidationError as exc:
        raise CatalogueError(f"agent tool catalogue invalid: {exc.message}") from exc
    seen: set[str] = set()
    out: list[AgentTool] = []
    for raw in doc["tools"]:
        name = raw["name"]
        if name in seen:
            raise CatalogueError(f"duplicate agent tool '{name}'")
        seen.add(name)
        params = dict(raw.get("params") or {"type": "object", "properties": {}})
        params.setdefault("properties", {})
        tool = AgentTool(
            name=name, description=" ".join(str(raw["description"]).split()),
            method=str(raw.get("method", "")), path=str(raw.get("path", "")),
            capability=raw["capability"],
            params=params, max_result_chars=int(raw.get("max_result_chars", 8000)),
            truncate=str(raw.get("truncate", "head")),
            opens_draft=bool(raw.get("opens_draft", False)),
            ui_action=bool(raw.get("ui_action", False)),
        )
        if tool.ui_action and (tool.method or tool.path):
            raise CatalogueError(f"tool '{name}': ui_action tools take no method/path")
        if not tool.ui_action and not (tool.method and tool.path):
            raise CatalogueError(f"tool '{name}': method and path are required")
        if tool.opens_draft and not tool.mutating:
            raise CatalogueError(f"tool '{name}': opens_draft requires a non-GET method")
        required = set(params.get("required") or [])
        for ph in tool.path_params:
            if ph not in params["properties"] or ph not in required:
                raise CatalogueError(
                    f"tool '{name}': path placeholder '{ph}' must be a required param")
        out.append(tool)
    return tuple(out)


@lru_cache(maxsize=1)
def load_catalogue() -> tuple[AgentTool, ...]:
    """Load + validate the bundled catalogue (cached).

    Raises:
        CatalogueError: On schema violation, duplicates or bad placeholders.
    """
    text = (resources.files("ovid2.resources") / "agent_tools" / "catalogue.yaml").read_text(
        encoding="utf-8")
    return _parse(yaml.safe_load(text))


def get_tool(name: str) -> AgentTool | None:
    """Return the catalogue entry named ``name`` (or ``None``)."""
    for tool in load_catalogue():
        if tool.name == name:
            return tool
    return None


def openai_tools(names: list[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """Return the OpenAI ``tools`` array, optionally restricted to ``names``."""
    allowed = set(names) if names is not None else None
    return [t.to_openai() for t in load_catalogue()
            if allowed is None or t.name in allowed]


def _coerce_args(tool: AgentTool, args: Any) -> dict[str, Any]:
    import jsonschema

    if args is None or args == "":
        args = {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError as exc:
            raise ToolCallError(f"arguments for '{tool.name}' are not valid JSON") from exc
    if not isinstance(args, dict):
        raise ToolCallError(f"arguments for '{tool.name}' must be a JSON object")
    known = set(tool.params.get("properties") or {})
    unknown = sorted(set(args) - known)
    if unknown:
        raise ToolCallError(f"unknown argument(s) for '{tool.name}': {', '.join(unknown)}")
    clean = {k: v for k, v in args.items() if v is not None}
    try:
        jsonschema.validate(clean, tool.params)
    except jsonschema.ValidationError as exc:
        raise ToolCallError(f"invalid arguments for '{tool.name}': {exc.message}") from exc
    return clean


def validate_args(tool: AgentTool, args: Any) -> dict[str, Any]:
    """Validate ``args`` against ``tool.params`` and return the clean dict.

    Used for UI actions, which have no request to build.

    Raises:
        ToolCallError: On unknown / invalid arguments.
    """
    return _coerce_args(tool, args)


def build_request(tool: AgentTool, args: Any) -> dict[str, Any]:
    """Map a tool call onto ``{method, path, query, body}``.

    Path placeholders are URL-encoded with no safe characters, so a model
    cannot traverse to a different route (``../secrets`` → ``..%2Fsecrets``).
    For GET the remaining arguments become query parameters; otherwise
    they form the JSON body.

    Raises:
        ToolCallError: On unknown / invalid / missing arguments, or when
            ``tool`` is a UI action (it has no request).
    """
    if tool.ui_action:
        raise ToolCallError(f"'{tool.name}' is a UI action; it has no HTTP request")
    clean = _coerce_args(tool, args)
    path = tool.path
    for ph in tool.path_params:
        value = clean.pop(ph, None)
        if value is None or str(value).strip() == "":
            raise ToolCallError(f"'{tool.name}' requires '{ph}'")
        path = path.replace("{" + ph + "}", quote(str(value), safe=""))
    if tool.method == "GET":
        query = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                 for k, v in clean.items()}
        return {"method": "GET", "path": path, "query": query, "body": None}
    return {"method": tool.method, "path": path, "query": {}, "body": clean}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(f"{len(load_catalogue())} agent tools load cleanly.")
