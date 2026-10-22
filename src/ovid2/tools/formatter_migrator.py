"""Migration utilities: legacy Ovid formatter class → Ovid2 YAML spec.

The legacy formatter shape is::

    from transformations.formatters.base import BaseFormatter

    INPUT_FILE_NAME = "*Condition_*.txt"

    class ConditionFormatter(BaseFormatter):
        @property
        def table_name(self) -> str:
            return "condition"

        @property
        def input_file_name(self) -> str:
            return INPUT_FILE_NAME

        def format(self, df: DataFrame) -> DataFrame:
            return df.select(
                df['PATID'].alias('PATID'),
                lit('').alias('ZIP_CODE'),
                ...
            )

This module inspects the source AST and produces a minimal
``pcornet_passthrough`` spec covering:

- ``sources.<table>.glob`` — from the ``INPUT_FILE_NAME`` module constant.
- ``projections`` — one entry per ``df['X'].alias('X')`` call (uppercase
  keys, lowercased SQL expressions to match the spec engine convention).
  Literal ``lit('')`` / ``lit(None)`` calls are converted to ``"''"`` and
  ``"CAST(NULL AS STRING)"`` respectively.

Formatters that do more than a flat ``df.select(...)`` (calls to
``withColumn``, joins, groupBy, etc.) are flagged as non-mechanical and
skipped — the operator must migrate them by hand with ``adapter: custom``.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

_ROOT_LEVEL_OPS: frozenset[str] = frozenset(
    {"withColumn", "groupBy", "join", "filter", "where", "unionByName"}
)

# Match names like ``ConditionFormatter`` -> ``condition``.
_TABLE_FROM_CLASS = re.compile(r"^([A-Z][A-Za-z0-9_]*)Formatter$")


# ---------------------------------------------------------------------------
# Analysis result model
# ---------------------------------------------------------------------------


class MigrationError(RuntimeError):
    """Raised when a formatter cannot be mechanically migrated."""


@dataclass
class FormatterAnalysis:
    """Everything the writer needs to emit a spec YAML."""

    pipeline: str
    table: str
    class_name: str
    input_glob: str
    projections: dict[str, str]
    warnings: list[str]


# ---------------------------------------------------------------------------
# AST inspection helpers
# ---------------------------------------------------------------------------


def _module_constants(tree: ast.Module) -> dict[str, ast.expr]:
    """Return every top-level ``NAME = <expr>`` assignment."""
    constants: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                constants[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                constants[node.target.id] = node.value
    return constants


def _string_value(node: ast.expr, constants: dict[str, ast.expr]) -> str | None:
    """Resolve ``node`` to a string literal, following one level of name refs."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in constants:
        return _string_value(constants[node.id], constants)
    return None


def _find_formatter_class(tree: ast.Module) -> ast.ClassDef:
    """Return the first ``*Formatter`` class definition; raise if none."""
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and _TABLE_FROM_CLASS.match(node.name):
            return node
    raise MigrationError("No <Table>Formatter class found in module.")


def _table_from_property(cls: ast.ClassDef) -> str | None:
    """Extract ``table_name`` string from the property, if present."""
    for item in cls.body:
        if not isinstance(item, ast.FunctionDef) or item.name != "table_name":
            continue
        for stmt in item.body:
            if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Constant):
                value = stmt.value.value
                if isinstance(value, str):
                    return value
    return None


def _extract_format_body(cls: ast.ClassDef) -> ast.FunctionDef:
    """Return the ``format`` method, or raise."""
    for item in cls.body:
        if isinstance(item, ast.FunctionDef) and item.name == "format":
            return item
    raise MigrationError(f"Class {cls.name} has no `format` method.")


def _find_select_call(fmt: ast.FunctionDef) -> ast.Call:
    """Locate the ``return df.select(...)`` call in the format method body.

    Rejects any method whose format is more complex than a single flat select.
    """
    complex_ops: list[str] = []
    top_level: list[ast.Call] = []
    for node in ast.walk(fmt):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr in _ROOT_LEVEL_OPS:
                complex_ops.append(attr)
            if attr == "select":
                top_level.append(node)
    if complex_ops:
        raise MigrationError(
            f"Non-mechanical formatter (uses {sorted(set(complex_ops))}); "
            f"cannot auto-migrate — use adapter: custom instead."
        )
    if not top_level:
        raise MigrationError("Format method has no `df.select(...)` call.")
    return top_level[-1]


def _projection_from_call(node: ast.expr) -> tuple[str, str] | None:
    """Turn one argument of ``df.select(...)`` into ``(OUT_NAME, sql_expr)``.

    Recognises three shapes:

    1. ``df['COL'].alias('OUT')``            -> ("OUT", "col")
    2. ``lit('value').alias('OUT')``         -> ("OUT", "'value'")
    3. ``lit(None).alias('OUT')``            -> ("OUT", "CAST(NULL AS STRING)")
    """
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    if node.func.attr != "alias":
        return None
    if len(node.args) != 1 or not isinstance(node.args[0], ast.Constant):
        return None
    alias_value = node.args[0].value
    if not isinstance(alias_value, str):
        return None

    target = node.func.value
    # Case 1: df['COL']
    if (
        isinstance(target, ast.Subscript)
        and isinstance(target.slice, ast.Constant)
        and isinstance(target.slice.value, str)
    ):
        return alias_value.upper(), target.slice.value.lower()

    # Case 2/3: lit(...)
    if isinstance(target, ast.Call) and _is_lit_call(target):
        if not target.args:
            return alias_value.upper(), "CAST(NULL AS STRING)"
        inner = target.args[0]
        if isinstance(inner, ast.Constant):
            if inner.value is None:
                return alias_value.upper(), "CAST(NULL AS STRING)"
            if isinstance(inner.value, str):
                escaped = inner.value.replace("'", "''")
                return alias_value.upper(), f"'{escaped}'"
            if isinstance(inner.value, (int, float, bool)):
                return alias_value.upper(), repr(inner.value)
    return None


def _is_lit_call(node: ast.Call) -> bool:
    """True if ``node`` is a call to ``lit(...)``."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "lit"
    if isinstance(func, ast.Attribute):
        return func.attr == "lit"
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyse(source: str, *, pipeline: str, module_path: str) -> FormatterAnalysis:
    """Parse ``source`` and return a :class:`FormatterAnalysis`.

    Args:
        source: The full Python source of a legacy formatter module.
        pipeline: Pipeline name (used for the resulting spec's ``pipeline`` field
            in memory — the YAML file itself doesn't record it).
        module_path: Path to the original file (for error messages only).

    Returns:
        Populated analysis result.

    Raises:
        MigrationError: If the formatter is non-mechanical or malformed.
    """
    try:
        tree = ast.parse(source, filename=module_path)
    except SyntaxError as exc:
        raise MigrationError(f"Cannot parse {module_path}: {exc}") from exc

    constants = _module_constants(tree)
    cls = _find_formatter_class(tree)

    match = _TABLE_FROM_CLASS.match(cls.name)
    assert match is not None  # by _find_formatter_class
    table_from_class = match.group(1).lower()
    # Prefer the explicit @property table_name value when present.
    table = _table_from_property(cls) or table_from_class

    input_glob_node = constants.get("INPUT_FILE_NAME")
    input_glob = _string_value(input_glob_node, constants) if input_glob_node else None
    if input_glob is None:
        raise MigrationError("Cannot resolve INPUT_FILE_NAME module constant.")

    fmt = _extract_format_body(cls)
    select_call = _find_select_call(fmt)

    projections: dict[str, str] = {}
    warnings: list[str] = []
    for arg in select_call.args:
        result = _projection_from_call(arg)
        if result is None:
            warnings.append(f"skipped non-mechanical argument: {ast.unparse(arg)}")
            continue
        out_name, sql = result
        projections[out_name] = sql

    if not projections:
        raise MigrationError("Failed to extract any projection from df.select(...).")

    return FormatterAnalysis(
        pipeline=pipeline,
        table=table,
        class_name=cls.name,
        input_glob=input_glob,
        projections=projections,
        warnings=warnings,
    )


def render_yaml_spec(analysis: FormatterAnalysis) -> str:
    """Render the analysis result as a YAML spec (stable ordering)."""
    lines: list[str] = [
        f"# Auto-generated from {analysis.class_name}. Review before committing.",
        "v: 1",
        f"formatter: {analysis.table}",
        "adapter: pcornet_passthrough",
        f"input_file_name: {_yaml_scalar(analysis.input_glob)}",
        "",
        "sources:",
        f"  {analysis.table}:",
        f"    glob: {_yaml_scalar(analysis.input_glob)}",
        "    lowercase_columns: true",
        "",
        "projections:",
    ]
    for key in sorted(analysis.projections):
        expr = analysis.projections[key]
        lines.append(f"  {key}: {_yaml_scalar(expr)}")
    lines.append("")
    return "\n".join(lines)


def _yaml_scalar(value: str) -> str:
    """Emit a YAML-safe scalar. Quotes if the value contains ambiguous chars."""
    if value == "":
        return "''"
    # Quote if the value contains any character that YAML might treat specially.
    special = set(":#-{}[]&*!|>%@`\",\n\t")
    if any(ch in special for ch in value) or value != value.strip():
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value


def migrate_file(
    source_path: Path,
    *,
    pipeline: str,
    destination_dir: Path,
    overwrite: bool = False,
) -> tuple[Path, FormatterAnalysis]:
    """Migrate one legacy formatter file to a YAML spec on disk.

    Args:
        source_path: Path to the legacy ``*_formatter.py`` file.
        pipeline: Pipeline name (used to compute the output path via
            :func:`ovid2.formatters.spec.find_and_load`).
        destination_dir: Directory to write ``<table>.yaml`` into.
        overwrite: When ``False`` and the target file exists, raise
            :class:`FileExistsError`.

    Returns:
        A tuple of ``(written_path, analysis)``.

    Raises:
        MigrationError: See :func:`analyse`.
        FileExistsError: If the destination already exists and
            ``overwrite`` is ``False``.
    """
    source = source_path.read_text(encoding="utf-8")
    analysis = analyse(source, pipeline=pipeline, module_path=str(source_path))
    destination_dir.mkdir(parents=True, exist_ok=True)
    target = destination_dir / f"{analysis.table}.yaml"
    if target.exists() and not overwrite:
        raise FileExistsError(f"{target} already exists (pass --overwrite to replace).")
    target.write_text(render_yaml_spec(analysis), encoding="utf-8")
    return target, analysis
