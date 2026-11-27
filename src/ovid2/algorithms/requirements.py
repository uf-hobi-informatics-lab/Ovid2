"""Static collection of what an algorithm calls — the basis for ``requires``.

:func:`used_capabilities` walks an algorithm's source (its class, or its
module for a health-check operator function) and returns the
:class:`~ovid2.config.capabilities.Capability` values it *uses*:

* every column function reached through :mod:`ovid2.core.engines.dsl`
  (``from ovid2.core.engines.dsl import col``, ``dsl.col``) or an engine
  functions namespace (``ctx.functions.col``, ``fn.col`` / ``fns.col`` /
  ``F.col`` bound from ``get_functions()``) → ``fn:<name>``;
* ``.over(...)`` / ``Window`` → ``window``;
* ``regexp_*`` / ``rlike`` → ``regex``;
* ``engine.checkpoint(...)`` / ``.checkpoint(`` → ``checkpoint``;
* ``broadcast(...)`` → ``broadcast_join``;
* ``input_file_name()`` → ``input_file_name``.

``tests/contract/test_algorithm_requires_declared.py`` asserts
``used_capabilities(algorithm) ⊆ algorithm.requires`` for every algorithm in
the :class:`~ovid2.algorithms.registry.AlgorithmRegistry`, so declarations
cannot go stale. Tier-neutral (``ast`` only).
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Any

from ovid2.config.capabilities import FUNCTION_PREFIX, Capability

__all__ = ["capabilities_in_source", "used_capabilities"]

_KNOWN_FUNCTIONS: frozenset[str] = frozenset(
    c.value[len(FUNCTION_PREFIX) :] for c in Capability if c.value.startswith(FUNCTION_PREFIX)
)

#: Names conventionally bound to an engine functions namespace.
_NAMESPACE_NAMES: frozenset[str] = frozenset({"F", "f", "fn", "fns", "functions", "sf", "psf"})

#: Function / method name → feature it implies.
_FEATURE_OF: dict[str, Capability] = {
    "over": Capability.WINDOW,
    "Window": Capability.WINDOW,
    "regexp_replace": Capability.REGEX,
    "regexp_extract": Capability.REGEX,
    "rlike": Capability.REGEX,
    "checkpoint": Capability.CHECKPOINT,
    "broadcast": Capability.BROADCAST_JOIN,
    "input_file_name": Capability.INPUT_FILE_NAME,
}


def _module_tree(obj: Any) -> ast.Module | None:
    module = inspect.getmodule(obj)
    if module is None:
        return None
    try:
        return ast.parse(inspect.getsource(module))
    except (OSError, TypeError):
        return None


def _object_tree(obj: Any) -> ast.AST | None:
    try:
        return ast.parse(textwrap.dedent(inspect.getsource(obj)))
    except (OSError, TypeError):
        return None


def _dsl_aliases(module: ast.Module) -> tuple[dict[str, str], set[str]]:
    """Return ``({local: dsl_name}, {module aliases of ovid2.core.engines.dsl})``."""
    names: dict[str, str] = {}
    modules: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom) and node.module == "ovid2.core.engines.dsl":
            for alias in node.names:
                names[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module == "ovid2.core.engines":
            for alias in node.names:
                if alias.name == "dsl":
                    modules.add(alias.asname or "dsl")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "ovid2.core.engines.dsl":
                    modules.add(alias.asname or alias.name)
    return names, modules


def capabilities_in_source(tree: ast.AST, module: ast.Module | None) -> frozenset[Capability]:
    """Return the capabilities used inside ``tree`` (``module`` resolves imports)."""
    dsl_names, dsl_modules = _dsl_aliases(module) if module is not None else ({}, set())
    # Function-local ``from ovid2.core.engines.dsl import …`` inside the tree.
    if isinstance(tree, ast.Module) or tree is not module:
        local_names, local_modules = _dsl_aliases(ast.Module(body=[tree], type_ignores=[]))  # type: ignore[list-item]
        dsl_names = {**dsl_names, **local_names}
        dsl_modules |= local_modules
    used: set[Capability] = set()

    def _fn(name: str) -> None:
        if name in _KNOWN_FUNCTIONS:
            used.add(Capability(f"{FUNCTION_PREFIX}{name}"))
        feature = _FEATURE_OF.get(name)
        if feature is not None:
            used.add(feature)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in dsl_names:
            _fn(dsl_names[node.id])
        elif isinstance(node, ast.Attribute):
            base = node.value
            via_namespace = (
                isinstance(base, ast.Name)
                and (base.id in dsl_modules or base.id in _NAMESPACE_NAMES)
            ) or (isinstance(base, ast.Attribute) and base.attr == "functions")
            if via_namespace:
                _fn(node.attr)
            elif node.attr in ("over", "rlike", "checkpoint") and isinstance(node.ctx, ast.Load):
                used.add(_FEATURE_OF[node.attr])
    return frozenset(used)


def used_capabilities(native: Any) -> frozenset[Capability]:
    """Return the capabilities ``native`` (a class or function) uses in its source.

    A class contributes its own body plus the module-level helper functions
    it calls by name; a function contributes its body and helpers likewise.
    """
    tree = _object_tree(native)
    if tree is None:
        return frozenset()
    module = _module_tree(native)
    used = set(capabilities_in_source(tree, module))
    if module is not None:
        helpers = {
            node.name: node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("_")
        }
        seen: set[str] = set()
        frontier = [tree]
        while frontier:
            current = frontier.pop()
            for node in ast.walk(current):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in helpers
                    and node.func.id not in seen
                ):
                    seen.add(node.func.id)
                    helper = helpers[node.func.id]
                    used |= capabilities_in_source(helper, module)
                    frontier.append(helper)
    return frozenset(used)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.algorithms.requirements is a library module.")
