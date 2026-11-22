"""Shared AST helpers for the contract suite — evasion-resistant import / call analysis.

Contract tests that match a single spelling (``import pyspark``,
``subprocess.run``) are easy to slip past by accident: ``import a, b``,
``from pkg import sub``, ``import x as y``, ``importlib.import_module("…")``,
``getattr(obj, "collect")``. These helpers normalise all of those so each
contract checks the *meaning*, not the spelling:

* :func:`iter_imports` — every module a file imports, fully qualified,
  covering multi-name imports, ``from pkg import sub`` (yields both
  ``pkg`` and ``pkg.sub``), relative imports, and whether the import sits
  under ``if TYPE_CHECKING:``.
* :func:`dynamic_imports` — string literals passed to
  ``importlib.import_module`` / ``__import__`` / ``importlib.util.find_spec``
  (plus non-literal calls, flagged so callers can reject them).
* :func:`alias_map` / :func:`resolved_call_name` — resolve a call through
  local import aliases back to its dotted origin
  (``sp.run`` → ``subprocess.run``, ``run`` → ``subprocess.run``).
* :func:`getattr_literals` — ``getattr(x, "name")`` attribute strings.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass

__all__ = [
    "DynamicImport",
    "ImportRef",
    "alias_map",
    "dynamic_imports",
    "getattr_literals",
    "iter_imports",
    "resolved_call_name",
    "type_checking_lines",
]


@dataclass(frozen=True)
class ImportRef:
    """One imported module reference."""

    module: str
    lineno: int
    type_checking: bool


@dataclass(frozen=True)
class DynamicImport:
    """One ``import_module`` / ``__import__`` / ``find_spec`` call."""

    module: str | None  # ``None`` when the argument is not a string literal
    lineno: int


_DYNAMIC_IMPORTERS: frozenset[str] = frozenset(
    {
        "importlib.import_module",
        "import_module",
        "__import__",
        "importlib.__import__",
        "importlib.util.find_spec",
        "find_spec",
    }
)


def _is_type_checking_test(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "TYPE_CHECKING") or (
        isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING"
    )


def type_checking_lines(tree: ast.AST) -> set[int]:
    """Return line numbers that sit inside an ``if TYPE_CHECKING:`` body."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking_test(node.test):
            for stmt in node.body:
                end = getattr(stmt, "end_lineno", stmt.lineno) or stmt.lineno
                lines.update(range(stmt.lineno, end + 1))
    return lines


def _resolve_relative(module: str | None, level: int, package: str | None) -> str:
    if level == 0:
        return module or ""
    if not package:
        return module or ""
    parts = package.split(".")
    base = parts[: len(parts) - (level - 1)] if level > 1 else parts
    return ".".join([*base, module] if module else base)


def iter_imports(tree: ast.AST, package: str | None = None) -> Iterator[ImportRef]:
    """Yield every imported module in ``tree``.

    Args:
        tree: Parsed module.
        package: Dotted package of the file (for resolving relative imports).

    Yields:
        One :class:`ImportRef` per name. ``from pkg import sub`` yields both
        ``pkg`` and ``pkg.sub`` (``sub`` may be a submodule).
    """
    tc = type_checking_lines(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield ImportRef(alias.name, node.lineno, node.lineno in tc)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative(node.module, node.level, package)
            if base:
                yield ImportRef(base, node.lineno, node.lineno in tc)
            for alias in node.names:
                if alias.name == "*":
                    continue
                full = f"{base}.{alias.name}" if base else alias.name
                yield ImportRef(full, node.lineno, node.lineno in tc)


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return f"{inner}.{node.attr}" if inner else None
    return None


def alias_map(tree: ast.AST) -> dict[str, str]:
    """Return ``{local_name: fully.qualified.origin}`` for every import binding."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    out[alias.asname] = alias.name
                else:
                    head = alias.name.split(".", 1)[0]
                    out.setdefault(head, head)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                if alias.name == "*":
                    continue
                out[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return out


def resolved_call_name(call: ast.Call, aliases: dict[str, str]) -> str | None:
    """Return the dotted origin of ``call.func`` after resolving import aliases."""
    dotted = _dotted(call.func)
    if dotted is None:
        return None
    head, _, rest = dotted.partition(".")
    origin = aliases.get(head, head)
    return f"{origin}.{rest}" if rest else origin


def dynamic_imports(tree: ast.AST) -> list[DynamicImport]:
    """Return every dynamic-import call with its literal module (or ``None``)."""
    aliases = alias_map(tree)
    found: list[DynamicImport] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = resolved_call_name(node, aliases)
        if name not in _DYNAMIC_IMPORTERS:
            continue
        first = node.args[0] if node.args else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.append(DynamicImport(first.value, node.lineno))
        else:
            found.append(DynamicImport(None, node.lineno))
    return found


def getattr_literals(tree: ast.AST) -> list[tuple[str, int]]:
    """Return ``(attr, lineno)`` for every ``getattr(x, "attr"[, d])`` call."""
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            out.append((node.args[1].value, node.lineno))
    return out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Imported by the contract tests.")
