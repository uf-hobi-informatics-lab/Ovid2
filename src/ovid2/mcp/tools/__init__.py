"""MCP tool registry.

Each ``*.py`` module in this package defines exactly one tool and
exposes four module-level attributes:

- ``TOOL_NAME: str`` — the MCP-visible tool name (snake_case).
- ``TOOL_DESCRIPTION: str`` — one-paragraph human-readable description.
- ``ARGS_MODEL: type[pydantic.BaseModel] | None`` — the argument shape,
  or ``None`` for a parameter-less tool.
- ``async def run(client, args) -> object`` — the tool implementation.
  ``args`` is either an :class:`ARGS_MODEL` instance or ``None`` (when
  ``ARGS_MODEL is None``). The return value is a JSON-serialisable
  object (dict, list, scalar) that the server wraps as
  :class:`mcp.types.TextContent`.

The contract test at ``tests/contract/test_mcp_tools_typed.py`` enforces
the ``ARGS_MODEL`` invariant at import time.

Rationale for one tool per module: it keeps the schema derivation
loop trivial (walk ``glob('*.py')``, ``importlib.import_module``, take
the four attributes), makes tool authorship self-contained, and makes
adding or removing a tool a single-file change.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel

if TYPE_CHECKING:
    from ovid2.mcp.client import WebApiClient

__all__ = ["ToolRunner", "ToolSpec", "discover_tools"]


class ToolRunner(Protocol):
    """Callable protocol every tool module implements."""

    def __call__(
        self, client: WebApiClient, args: BaseModel | None
    ) -> Awaitable[Any]:  # pragma: no cover - Protocol
        ...


@dataclass(frozen=True)
class ToolSpec:
    """One entry in the tool registry."""

    name: str
    description: str
    args_model: type[BaseModel] | None
    run: ToolRunner
    module: str


def _load(module_name: str) -> ToolSpec | None:
    """Import ``module_name`` and construct a :class:`ToolSpec`.

    Returns ``None`` when the module is missing any required attribute
    (typically the ``__init__.py`` itself, which has none).
    """
    module = importlib.import_module(module_name)
    name = getattr(module, "TOOL_NAME", None)
    description = getattr(module, "TOOL_DESCRIPTION", None)
    args_model = getattr(module, "ARGS_MODEL", ...)  # sentinel — explicit None allowed
    runner: Callable[..., Any] | None = getattr(module, "run", None)
    if not name or not description or args_model is ... or runner is None:
        return None
    return ToolSpec(
        name=name,
        description=description,
        args_model=args_model,
        run=runner,
        module=module_name,
    )


def discover_tools() -> list[ToolSpec]:
    """Return every tool discoverable under :mod:`ovid2.mcp.tools`.

    The returned list is sorted by ``name`` so the tool ordering the
    MCP client sees is stable across runs.
    """
    specs: list[ToolSpec] = []
    package = importlib.import_module(__name__)
    for _, mod_name, is_pkg in pkgutil.iter_modules(package.__path__):
        if is_pkg or mod_name.startswith("_"):
            continue
        spec = _load(f"{__name__}.{mod_name}")
        if spec is not None:
            specs.append(spec)
    specs.sort(key=lambda s: s.name)
    return specs
