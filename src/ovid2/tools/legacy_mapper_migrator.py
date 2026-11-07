"""Migrate legacy-Ovid mapper scripts to ``legacy_select`` mapper YAML.

Legacy mappers live in ``<legacy-ovid><family>_mappers/``
and are *shared* by every partner of that family. Each one is a single
``unmapped.select(...)`` (optionally on a geocoding join) plus an optional
``cf.append_additional_fields``. This tool parses the script with ``ast``
(never imports it) and emits the equivalent
``resources/cdms/<cdm>/mappers/<table>.yaml``.

Usage::

    python -m ovid2.tools.legacy_mapper_migrator \\
        --legacy-dir <legacy-ovid> \\
        --out-dir src/ovid2/resources/cdms/ovid2_pipeline_veritas_v1/mappers [--write]

Scripts whose select uses a construct outside the ``legacy_select``
grammar are reported and skipped (they need a ``class:`` mapper).
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class MapperMigrationError(ValueError):
    """Raised when a legacy mapper uses a construct outside the grammar."""


@dataclass
class MapperAnalysis:
    """Result of analysing one legacy mapper script."""

    table: str
    columns: list[Any]
    join_field: str | None
    dicts: dict[str, str] = field(default_factory=dict)  # map var → dictionaries attr
    needs_geocoding: bool = False
    source: str = ""


def _str(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise MapperMigrationError(f"expected a string literal, got {ast.unparse(node)}")


def _call_name(node: ast.AST) -> str:
    return ast.unparse(node.func) if isinstance(node, ast.Call) else ""


def _subscript_col(node: ast.AST) -> str | None:
    """``unmapped['X']`` / ``joined['X']`` → ``X``."""
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        value = node.slice.value
        if isinstance(value, str):
            return value
    return None


def _col_call(node: ast.AST) -> str | None:
    """``col('X')`` → ``X``."""
    if isinstance(node, ast.Call) and _call_name(node) == "col" and len(node.args) == 1:
        return _str(node.args[0])
    return None


def _column_ref(node: ast.AST) -> str | None:
    return _subscript_col(node) or _col_call(node)


class _Analyser:
    def __init__(self, tree: ast.Module, source: str) -> None:
        self.tree = tree
        self.source = source
        self.dicts: dict[str, str] = {}
        self.local_dicts: set[str] = set()
        self.udfs: dict[str, str] = {}
        self._collect_maps()

    def _collect_maps(self) -> None:
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            src = ast.unparse(node.value)
            if src.startswith("create_map("):
                # create_map([lit(x) for x in chain(*partner_dictionaries.<attr>.items())])
                for sub in ast.walk(node.value):
                    if (
                        isinstance(sub, ast.Attribute)
                        and isinstance(sub.value, ast.Name)
                        and sub.value.id == "partner_dictionaries"
                    ):
                        self.dicts[target.id] = sub.attr
                        break
                else:
                    # cf.get_xxx() geocoding mappings — resolved by name.
                    for sub in ast.walk(node.value):
                        if (
                            isinstance(sub, ast.Attribute)
                            and isinstance(sub.value, ast.Name)
                            and sub.value.id == "cf"
                        ):
                            self.dicts[target.id] = f"@geo:{sub.attr}"
                            break
            elif src.startswith("udf("):
                fn = (
                    node.value.args[0]
                    if isinstance(node.value, ast.Call) and node.value.args
                    else None
                )
                if isinstance(fn, ast.Name):
                    self.udfs[target.id] = fn.id

    def _dict_name(self, var: str) -> str:
        if var not in self.dicts:
            raise MapperMigrationError(f"unknown mapping variable '{var}'")
        return self.dicts[var]

    def _lookup(self, node: ast.AST) -> tuple[str, str, bool] | None:
        """``MAP[upper(col('X'))]`` → (dict, X, upper=True); ``MAP[col('X')]`` → upper False."""
        if not (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)):
            return None
        var = node.value.id
        key = node.slice
        if isinstance(key, ast.Call) and _call_name(key) == "upper" and len(key.args) == 1:
            c = _column_ref(key.args[0])
            if c is not None:
                return self._dict_name(var), c, True
        c = _column_ref(key)
        if c is not None:
            return self._dict_name(var), c, False
        return None

    def expr(self, node: ast.AST) -> Any:
        c = _column_ref(node)
        if c is not None:
            return {"col": c}
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if isinstance(node.func, ast.Attribute) and node.func.attr == "cast":
                inner = _column_ref(node.func.value)
                if inner is not None:
                    return {"col": inner, "cast": _str(node.args[0])}
            if name == "cf.encrypt_id_udf" and len(node.args) == 1:
                arg = node.args[0]
                ref = _column_ref(arg)
                if ref is not None:
                    return {"encrypt": ref}
                if isinstance(arg, ast.Call) and _call_name(arg) == "concat" and len(arg.args) == 2:
                    ref = _column_ref(arg.args[0])
                    lit = arg.args[1]
                    if ref and isinstance(lit, ast.Call) and _call_name(lit) == "lit":
                        return {"encrypt": ref, "suffix": _str(lit.args[0])}
            if name == "cf.get_current_time_udf":
                return {"updated": True}
            if name == "lit" and len(node.args) == 1:
                lit_arg = node.args[0]
                if ast.unparse(lit_arg) == "input_partner.upper()":
                    return {"source": True}
                if isinstance(lit_arg, ast.Constant):
                    return {"lit": lit_arg.value}
            if name == "when" and len(node.args) == 2:
                cond, value = node.args
                if (
                    isinstance(cond, ast.Compare)
                    and len(cond.ops) == 1
                    and isinstance(cond.ops[0], ast.NotEq)
                    and ast.unparse(cond.comparators[0]) in ("lit('')", 'lit("")')
                ):
                    left = _column_ref(cond.left)
                    if left is not None and _column_ref(value) == left:
                        return {"nonblank": left}
            if name == "coalesce":
                parts = node.args
                if len(parts) == 2:
                    lk = self._lookup(parts[0])
                    if lk is not None:
                        d, col_name, upper = lk
                        fb = parts[1]
                        out: dict[str, Any] = {"dict": d, "col": col_name}
                        if not upper:
                            out["upper"] = False
                        if _column_ref(fb) == col_name:
                            return out
                        fb_expr = self.expr(fb)
                        if isinstance(fb_expr, dict) and "lit" in fb_expr:
                            out["fallback"] = fb_expr
                            return out
                subs: list[Any] = []
                for p in parts:
                    lk = self._lookup(p)
                    if lk is not None:
                        d, col_name, upper = lk
                        item: dict[str, Any] = {"dict_only": d, "col": col_name}
                        if not upper:
                            item["upper"] = False
                        subs.append(item)
                    else:
                        subs.append(self.expr(p))
                return {"coalesce": subs}
            if isinstance(node.func, ast.Name) and node.func.id in self.udfs:
                args = [_column_ref(a) for a in node.args]
                if all(a is not None for a in args):
                    return {"udf": self.udfs[node.func.id], "args": args}
        lk = self._lookup(node)
        if lk is not None:
            d, col_name, upper = lk
            out = {"dict": d, "col": col_name, "fallback": "none"}
            if not upper:
                out["upper"] = False
            return out
        raise MapperMigrationError(f"unsupported expression: {ast.unparse(node)}")

    def item(self, node: ast.AST) -> tuple[str, Any]:
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "alias"
        ):
            return _str(node.args[0]), self.expr(node.func.value)
        ref = _subscript_col(node)
        if ref is not None:
            return ref, {"col": ref}
        raise MapperMigrationError("select item without alias: " + ast.unparse(node))


def analyse(source: str, *, table: str) -> MapperAnalysis:
    """Analyse one legacy mapper script."""
    tree = ast.parse(source)
    an = _Analyser(tree, source)
    selects = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "select"
        and isinstance(n.func.value, ast.Name)
        and (n.func.value.id.startswith("unmapped_") or n.func.value.id == "joined_geocoding")
    ]
    if len(selects) != 1:
        raise MapperMigrationError(f"expected exactly one mapping select, found {len(selects)}")
    columns: list[Any] = []
    join_field: str | None = None
    for arg in selects[0].args:
        out, expr = an.item(arg)
        if out == "JOIN_FIELD":
            continue
        if expr == {"col": out}:
            columns.append(out)
        else:
            columns.append({out: expr})
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) == "cf.append_additional_fields":
            for kw in node.keywords:
                if kw.arg == "join_field":
                    join_field = _str(kw.value)
    geo = any(v.startswith("@geo:") for v in an.dicts.values()) or "joined_geocoding" in source
    return MapperAnalysis(
        table=table,
        columns=columns,
        join_field=join_field,
        dicts=an.dicts,
        needs_geocoding=geo,
        source=source,
    )


def _direct_backslash_read(source: str) -> bool:
    """True when the mapper reads its input with ``spark.read…load`` (no ``escape``).

    Legacy ``cf.spark_read`` passes ``escape='"'``; a direct
    ``spark.read.load(…, quote='"')`` keeps Spark's backslash escape.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "load"
            and "spark.read" in ast.unparse(node.func)
            and "deduplicated" in ast.unparse(node.args[0] if node.args else node)
        ):
            return not any(kw.arg == "escape" for kw in node.keywords)
    return False


def render(analysis: MapperAnalysis, *, origin: str) -> str:
    """Render the mapper YAML for ``analysis``."""
    op: dict[str, Any] = {"op": "legacy_select"}
    if "joined_geocoding" in analysis.source and "geocoding_zip5" in analysis.source:
        op["pre_join"] = "geocoding"
    if _direct_backslash_read(analysis.source):
        op["reader"] = "backslash_escape"
    op["columns"] = analysis.columns
    if analysis.join_field:
        op["append_additional_fields"] = {"join_field": analysis.join_field}
    payload = {"v": 1, "table": analysis.table, "ops": [op]}
    header = (
        f"# Ported verbatim from legacy {origin} by ovid2.tools.legacy_mapper_migrator.\n"
        "# Do not hand-edit: re-run the migrator when the legacy mapper changes.\n"
    )
    return header + yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=120)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(prog="legacy-mapper-migrator")
    parser.add_argument("--legacy-dir", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    legacy = Path(args.legacy_dir)
    out = Path(args.out_dir)
    failures = 0
    for script in sorted(legacy.glob("*_mapper.py")):
        table = script.name[: -len("_mapper.py")]
        try:
            analysis = analyse(script.read_text(encoding="utf-8"), table=table)
        except (MapperMigrationError, SyntaxError) as exc:
            failures += 1
            print(f"[SKIP] {script.name}: {exc}")
            continue
        flag = " (geocoding)" if analysis.needs_geocoding else ""
        print(f"[ OK ] {script.name} -> {table}.yaml  cols={len(analysis.columns)}{flag}")
        if args.write:
            out.mkdir(parents=True, exist_ok=True)
            origin = f"mapping_scripts/{legacy.name}/{script.name}"
            (out / f"{table}.yaml").write_text(render(analysis, origin=origin), encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
