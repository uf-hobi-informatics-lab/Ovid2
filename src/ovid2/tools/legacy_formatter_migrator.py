"""Port legacy-Ovid partner formatter scripts to Ovid2 ``formatters_custom``.

A legacy formatter (``<legacy-ovid><p>/formatting_scripts/
<t>_formatter.py``) is a CLI script::

    <imports>
    parser = argparse.ArgumentParser(); …; cf = CommonFuncitons(partner)
    spark = cf.get_spark_session("ovid")
    <module-level helper defs / udf(...) assignments>
    try:
        <body: reads, transforms, cf.write_pyspark_output_file(...)>
    except Exception as e:
        spark.stop(); cf.print_failure_message(...)

The migrator keeps the imports, the helper defs and the ``try`` body
verbatim and emits a module with a :class:`ovid2.legacy.formatter.LegacyFormatter`
subclass whose ``BODY`` is::

    def _legacy_body(rt, cf, spark):
        input_data_folder = rt.folder_label
        partner_name = <pipeline>
        <try body, with paths rewritten>

Rewrites (AST-level, on string constants and f-strings only):

* ``/data/{input_data_folder}/`` → ``rt.input_dir``
* ``/app/partners/{p}/data/{folder}/formatter_output/`` → ``rt.work_dir``
* ``/app/partners/{p}/data/datavant_output/{folder}/`` → ``rt.datavant_dir``
* ``/app/common/omop_cdm/`` → ``rt.omop_dir``; ``/app/common/lists/`` and
  the relative ``common/lists/`` → ``rt.lists_dir``
* ``/app/partners/{p}/dictionaries.py`` → ``rt.dictionaries_path()``
* ``importlib.import_module("partners." + p + ".x")`` → ``rt.module_name(...)``
* ``spark.stop()`` → removed
* ``<expr>.strftime(fmt)`` → ``legacy_strftime(<expr>, fmt)`` (CPython 3.10
  unpadded ``%Y``; see :func:`ovid2.legacy.udfs.legacy_strftime`)
* module-level ``cf``/``spark`` uses inside helper defs keep working
  because the generated module binds them per call.

The ``except`` arm is dropped: an exception propagates to the Ovid2 table
runner (legacy swallowed it and printed a message — the table simply had
no ``formatted_<t>.csv``; Ovid2 marks the table failed instead).

Usage::

    python -m ovid2.tools.legacy_formatter_migrator \\
        --legacy-partner-dir <legacy-ovid> \\
        --pipeline pi --pipelines-root pipelines/ovid2_pipelines [--write]
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: Module-level statements that belong to the legacy CLI harness.
_HARNESS_TARGETS: frozenset[str] = frozenset(
    {"parser", "args", "input_data_folder", "partner_name", "cf", "spark"}
)
#: Legacy ``common/`` modules re-pointed at :mod:`ovid2.legacy.compat`.
_COMPAT_MODULES: frozenset[str] = frozenset({"pcornet_cdm", "ovid_logging", "settings"})
_SKIP_EXPR_PREFIXES: tuple[str, ...] = ("parser.add_argument", "spark.conf.set", "spark.stop")


class FormatterMigrationError(ValueError):
    """Raised when a legacy formatter does not follow the script shape."""


@dataclass
class FormatterPort:
    """Result of porting one legacy formatter."""

    table: str
    output_file: str
    input_glob: str
    source: str
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Path rewriting
# ---------------------------------------------------------------------------

_RT_ATTR = {
    "input": "input_dir",
    "work": "work_dir",
    "datavant": "datavant_dir",
    "omop": "omop_dir",
    "lists": "lists_dir",
}

# (regex over the f-string "template", where {..} placeholders are kept as
# "{}"), → rt attribute. Applied to the *prefix* of the string.
_PREFIXES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^/data/\{\}/datavant_output/"), "datavant"),
    (re.compile(r"^/data/\{\}/"), "input"),
    (re.compile(r"^/app/partners/[^/]*(\{\})?[^/]*/data/datavant_output/\{\}/"), "datavant"),
    (re.compile(r"^/app/partners/[^/]*(\{\})?[^/]*/data/\{\}/formatter_output/"), "work"),
    (re.compile(r"^/app/common/omop_cdm/"), "omop"),
    (re.compile(r"^/app/common/lists/"), "lists"),
    (re.compile(r"^common/lists/"), "lists"),
]


def _template(node: ast.AST) -> tuple[str, list[ast.expr]] | None:
    """Return ``(template, placeholders)`` for a str constant / f-string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.replace("{", "{{").replace("}", "}}"), []
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        holes: list[ast.expr] = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value.replace("{", "{{").replace("}", "}}"))
            elif isinstance(v, ast.FormattedValue):
                parts.append("{}")
                holes.append(v)  # type: ignore[arg-type]
            else:
                return None
        return "".join(parts), holes
    return None


def _rebuild(rest_template: str, holes: list[ast.expr]) -> ast.expr:
    """Rebuild a str/f-string AST node from a template and its holes."""
    pieces = re.split(r"(?<!\{)\{\}(?!\})", rest_template)
    values: list[ast.expr] = []
    for i, piece in enumerate(pieces):
        text = piece.replace("{{", "{").replace("}}", "}")
        if text:
            values.append(ast.Constant(value=text))
        if i < len(holes):
            values.append(holes[i])
    if not holes:
        return ast.Constant(value="".join(v.value for v in values if isinstance(v, ast.Constant)))
    return ast.JoinedStr(values=values)


class _PathRewriter(ast.NodeTransformer):
    """Rewrite legacy absolute paths into ``rt.<dir>`` expressions."""

    def __init__(self) -> None:
        self.unhandled: list[str] = []

    def _rewrite(self, node: ast.expr) -> ast.expr:
        tpl = _template(node)
        if tpl is None:
            return node
        template, holes = tpl
        for pattern, kind in _PREFIXES:
            m = pattern.match(template)
            if not m:
                continue
            consumed = template[: m.end()].count("{}") - template[: m.end()].count("{{}}")
            rest = template[m.end() :]
            rest_holes = holes[consumed:]
            # A plain-string local (bound at the top of ``_legacy_body``), not
            # ``rt.<dir>``: helper functions turned into UDFs close over it and
            # must not pickle the runtime (it holds the SparkSession).
            base = ast.Name(id=f"_rt_{_RT_ATTR[kind]}", ctx=ast.Load())
            if not rest:
                return base
            return ast.BinOp(left=base, op=ast.Add(), right=_rebuild(rest, rest_holes))
        if re.match(r"^/app/partners/[^/]*(\{\})?[^/]*/dictionaries\.py$", template):
            return ast.parse("rt.dictionaries_path()", mode="eval").body
        if template.startswith(("/app/", "/data/")):  # noqa: OVID2002 — legacy literals being detected
            self.unhandled.append(template)
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        return self._rewrite(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.AST:
        return self._rewrite(node)

    def visit_Call(self, node: ast.Call) -> ast.AST:
        # ``<expr>.strftime(fmt)`` → ``legacy_strftime(<expr>, fmt)``: legacy
        # ran CPython 3.10 (unpadded ``%Y`` for years < 1000); see
        # :func:`ovid2.legacy.udfs.legacy_strftime`. ``time.strftime`` /
        # ``datetime.strftime`` (module/class form) are left alone.
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "strftime"
            and ast.unparse(node.func.value)
            not in ("time", "datetime", "date", "datetime.datetime")
            and len(node.args) == 1
            and not node.keywords
        ):
            self.generic_visit(node)
            func = node.func
            assert isinstance(func, ast.Attribute)
            return ast.Call(
                func=ast.Name(id="legacy_strftime", ctx=ast.Load()),
                args=[func.value, node.args[0]],
                keywords=[],
            )
        # importlib.import_module("partners." + p + ".partner_settings") etc.
        if ast.unparse(node.func) == "importlib.import_module" and node.args:
            arg_src = ast.unparse(node.args[0])
            if "partners" in arg_src or not isinstance(node.args[0], ast.Constant):
                node.args[0] = ast.Call(
                    func=ast.parse("rt.module_name", mode="eval").body,
                    args=[node.args[0]],
                    keywords=[],
                )
                return node
        self.generic_visit(node)
        return node

    def visit_Expr(self, node: ast.Expr) -> ast.AST | None:
        if isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == "spark.stop":
            return None
        self.generic_visit(node)
        return node


# ---------------------------------------------------------------------------
# Script analysis
# ---------------------------------------------------------------------------


def _is_harness(stmt: ast.stmt) -> bool:
    if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        names = {t.id for t in targets if isinstance(t, ast.Name)}
        return bool(names) and names <= _HARNESS_TARGETS
    if isinstance(stmt, ast.Expr):
        src = ast.unparse(stmt)
        return src.startswith(_SKIP_EXPR_PREFIXES)
    return False


def _find_output(body: list[ast.stmt]) -> tuple[str, list[str]]:
    """Return the ``formatted_<t>.csv`` written by the body, plus other outputs."""
    outs: list[str] = []
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(
            "write_pyspark_output_file"
        ):
            for kw in node.keywords:
                if kw.arg == "output_file_name":
                    tpl = _template(kw.value)
                    if tpl is not None and not tpl[1]:
                        outs.append(tpl[0].replace("{{", "{").replace("}}", "}"))
            if len(node.args) >= 2:
                tpl = _template(node.args[1])
                if tpl is not None and not tpl[1]:
                    outs.append(tpl[0])
    formatted = [o for o in outs if o.startswith("formatted_")]
    return (formatted[-1] if formatted else ""), outs


def _copied_output(body: list[ast.stmt]) -> str:
    """``cf.copy_data(src, "formatted_<t>.csv", …)`` — output produced by a copy."""
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if (
            isinstance(node, ast.Call)
            and ast.unparse(node.func) == "cf.copy_data"
            and len(node.args) >= 2
        ):
            tpl = _template(node.args[1])
            if tpl is not None and not tpl[1] and tpl[0].startswith("formatted_"):
                return tpl[0]
    return ""


def _first_glob(body: list[ast.stmt]) -> str:
    """Best-effort primary input glob (used only for the placeholder read)."""
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if (
                isinstance(t, ast.Name)
                and t.id.endswith("_table_name")
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                return node.value.value
    return "*"


_HEADER = '''"""Ported verbatim from legacy ``{origin}``.

Generated by :mod:`ovid2.tools.legacy_formatter_migrator` — do not hand-edit;
re-run the migrator when the legacy formatter changes. The body below is the
legacy script's ``try:`` block with legacy paths rewritten to ``rt.*``.
"""

# ruff: noqa
# mypy: ignore-errors
# fmt: off
# ovid2: legacy-port — eager actions allowed (``legacy_ovid`` profile, see
# tests/contract/test_legacy_ports_contained.py).

from ovid2.legacy.formatter import LegacyFormatter
from ovid2.legacy.udfs import legacy_strftime
{imports}

{helpers}

def _legacy_body(rt, cf, spark):
    CommonFuncitons = cf.factory  # legacy re-instantiates ``CommonFuncitons(...)``
    globals()["CommonFuncitons"] = CommonFuncitons
    globals()["cf"] = cf
    globals()["spark"] = spark
    _rt_input_dir, _rt_work_dir, _rt_datavant_dir = rt.input_dir, rt.work_dir, rt.datavant_dir
    _rt_omop_dir, _rt_lists_dir = rt.omop_dir, rt.lists_dir
    input_data_folder = rt.folder_label
    partner_name = {pipeline!r}
{body}


class {cls}(LegacyFormatter):
    """Legacy-parity formatter for ``{table}``."""

    TABLE = {table!r}
    INPUT_GLOB = {glob!r}
    OUTPUT_FILE = {output!r}
    BODY = staticmethod(_legacy_body)
'''


def _assigned_names(stmt: ast.stmt) -> set[str]:
    targets: list[ast.expr] = []
    if isinstance(stmt, ast.Assign):
        targets = list(stmt.targets)
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets = [stmt.target]
    return {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}


def _reads_any(stmt: ast.stmt, names: set[str]) -> bool:
    if not names or isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return False
    return any(isinstance(n, ast.Name) and n.id in names for n in ast.walk(stmt))


#: Calls that need an active SparkContext when evaluated at import time.
_SESSION_CALLS: tuple[str, ...] = ("Window.", "col(", "lit(", "broadcast(", "F.", "expr(", "when(")


def _needs_session(stmt: ast.stmt) -> bool:
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return False
    if not isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.Expr)):
        return False
    src = ast.unparse(stmt)
    return any(tok in src for tok in _SESSION_CALLS)


def _uses_harness(stmt: ast.stmt) -> bool:
    """True when a non-def module statement reads a per-call harness name."""
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return False
    names = {
        n.id for n in ast.walk(stmt) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    if names & {"partner_name", "input_data_folder", "spark", "cf", "args"}:
        return True
    src = ast.unparse(stmt)
    return "/app/" in src or "/data/" in src  # noqa: OVID2002 — legacy literals being detected


def _writes_output(node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Call) and ast.unparse(n.func).endswith("write_pyspark_output_file")
        for n in ast.walk(node)
    )


def _function_entry(tree: ast.Module) -> tuple[ast.FunctionDef, list[ast.stmt]] | None:
    """Shape B: ``def main(...)`` / ``def format_x(folder, partner)`` + ``__main__`` guard.

    Returns the entry function and its (try-unwrapped) body with the
    function-local ``cf = …`` / ``spark = …`` harness lines removed.
    """
    if any(isinstance(s, ast.Try) and _writes_output(s) for s in tree.body):
        return None
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef) and _writes_output(stmt):
            inner: list[ast.stmt] = []
            for s in stmt.body:
                if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant):
                    continue  # docstring
                if _is_harness(s):
                    continue
                if isinstance(s, ast.Try) and _writes_output(s):
                    inner.extend(s.body)
                else:
                    inner.append(s)
            return stmt, inner
    return None


def port_formatter(source: str, *, pipeline: str, origin: str) -> FormatterPort:
    """Port one legacy formatter script.

    Raises:
        FormatterMigrationError: When the script does not follow the shape.
    """
    tree = ast.parse(source)
    imports: list[str] = []
    helpers: list[str] = []
    prologue: list[ast.stmt] = []
    prologue_names: set[str] = set()
    body: list[ast.stmt] | None = None
    entry = _function_entry(tree)
    if entry is not None:
        entry_fn, body = entry
        tree.body = [
            s
            for s in tree.body
            if s is not entry_fn
            and not (isinstance(s, ast.If) and "__main__" in ast.unparse(s.test))
        ]
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            if isinstance(stmt, ast.ImportFrom) and stmt.module == "commonFunctions":
                continue
            if isinstance(stmt, ast.ImportFrom) and stmt.module in _COMPAT_MODULES:
                stmt.module = f"ovid2.legacy.compat.{stmt.module}"
            imports.append(ast.unparse(stmt))
        elif _is_harness(stmt):
            continue
        elif isinstance(stmt, ast.Try) and body is None and _writes_output(stmt):
            body = list(stmt.body)
        elif isinstance(stmt, ast.Try):
            helpers.append(ast.unparse(stmt))
        elif _uses_harness(stmt) or _reads_any(stmt, prologue_names) or _needs_session(stmt):
            # Module-level statement that needs the per-call runtime
            # (``partner_name`` / ``spark`` / ``cf`` / paths / an active
            # Spark session), or depends on one that does: run it at the
            # start of the body, in source order, and expose it globally
            # so module-level helper defs still see it.
            prologue.append(stmt)
            prologue_names |= _assigned_names(stmt)
        else:
            helpers.append(ast.unparse(stmt))
    if body is None:
        # A few legacy formatters have no try/except — the whole script is the body.
        body = [
            s
            for s in tree.body
            if not isinstance(s, (ast.Import, ast.ImportFrom)) and not _is_harness(s)
        ]
        helpers = [h for h in helpers if h.startswith(("def ", "@", "class "))]
        body = [s for s in body if not isinstance(s, (ast.FunctionDef, ast.ClassDef))]
        # The whole script already is the body, in source order — the
        # prologue statements are in it; prepending them would run the
        # script's first half twice (and read names before they are bound).
        prologue = []

    body = prologue + body
    global_names = sorted(prologue_names)
    rewriter = _PathRewriter()
    body_mod = rewriter.visit(ast.Module(body=body, type_ignores=[]))
    ast.fix_missing_locations(body_mod)
    helper_mod = rewriter.visit(ast.parse("\n".join(helpers))) if helpers else None

    output, outs = _find_output(body)
    if not output:
        output = _copied_output(body)
    if not output:
        raise FormatterMigrationError(f"no formatted_<t>.csv output found (outputs: {outs})")
    table = output[len("formatted_") : -len(".csv")]
    warnings = [f"unrewritten path literal: {p}" for p in rewriter.unhandled]

    body_src = ast.unparse(body_mod) or "pass"
    body_src = "\n".join("    " + line if line else line for line in body_src.splitlines())
    helpers_src = ast.unparse(helper_mod) if helper_mod is not None else ""
    if helpers_src:
        helpers_src = "rt = None  # bound per call in _legacy_body\n" + helpers_src
    cls = "".join(p.capitalize() for p in table.split("_")) + "Formatter"
    code = _HEADER.format(
        origin=origin,
        imports="\n".join(imports),
        helpers=helpers_src,
        pipeline=pipeline,
        body=body_src.replace("\n    ", "\n    ", 1),
        cls=cls,
        table=table,
        glob=_first_glob(body),
        output=output,
    )
    rt_locals = "_rt_input_dir, _rt_work_dir, _rt_datavant_dir, _rt_omop_dir, _rt_lists_dir"
    code = code.replace(
        '    globals()["spark"] = spark\n',
        '    globals()["spark"] = spark\n    globals()["rt"] = rt\n'
        + f"    global {', '.join([*global_names, rt_locals])}\n",
    )
    compile(code, origin, "exec")
    return FormatterPort(
        table=table,
        output_file=output,
        input_glob=_first_glob(body),
        source=code,
        warnings=warnings,
    )


def _class_name_for(table: str) -> str:
    """Return the class name ``ovid2.core.loaders.load_formatter`` looks up."""
    return f"{table.capitalize()}Formatter"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(prog="legacy-formatter-migrator")
    parser.add_argument("--legacy-partner-dir", required=True)
    parser.add_argument("--pipeline", required=True)
    parser.add_argument("--pipelines-root", required=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--tables", nargs="*", default=None)
    args = parser.parse_args(argv)

    legacy = Path(args.legacy_partner_dir) / "formatting_scripts"
    dest = Path(args.pipelines_root) / args.pipeline / "formatters_custom"
    failures = 0
    seen: dict[str, str] = {}
    for script in sorted(legacy.glob("*_formatter.py")):
        if (
            script.stem.endswith(("_old", "_retired"))
            or "_old" in script.stem
            or "retired" in script.stem
        ):
            continue
        stem = script.name[: -len("_formatter.py")]
        if args.tables is not None and stem not in args.tables:
            continue
        origin = f"partners/{Path(args.legacy_partner_dir).name}/formatting_scripts/{script.name}"
        try:
            port = port_formatter(
                script.read_text(encoding="utf-8"), pipeline=args.pipeline, origin=origin
            )
        except (FormatterMigrationError, SyntaxError) as exc:
            failures += 1
            print(f"[FAIL] {script.name}: {exc}")
            continue
        if port.table in seen:
            print(
                f"[WARN] {script.name}: table {port.table} already produced by {seen[port.table]}"
            )
        seen[port.table] = script.name
        src = port.source.replace(
            f"class {''.join(p.capitalize() for p in port.table.split('_'))}Formatter(",
            f"class {_class_name_for(port.table)}(",
        )
        print(f"[ OK ] {script.name} -> formatters_custom/{port.table}_formatter.py")
        for w in port.warnings:
            print(f"       warning: {w}")
        if args.write:
            dest.mkdir(parents=True, exist_ok=True)
            init = dest / "__init__.py"
            if not init.exists():
                init.write_text(
                    '"""Legacy-parity formatter ports (generated)."""\n', encoding="utf-8"
                )
            (dest / f"{port.table}_formatter.py").write_text(src, encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
