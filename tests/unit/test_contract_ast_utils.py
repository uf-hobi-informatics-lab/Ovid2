"""Unit tests for ``tests/contract/_ast_utils`` — the evasion cases the contracts rely on."""

from __future__ import annotations

import ast

import pytest

from tests.contract._ast_utils import (
    alias_map,
    dynamic_imports,
    getattr_literals,
    iter_imports,
    resolved_call_name,
)


def _mods(src: str, package: str | None = None) -> set[str]:
    return {r.module for r in iter_imports(ast.parse(src), package)}


@pytest.mark.unit
def test_multi_name_and_from_sub_imports() -> None:
    assert {"json", "ovid2.core"} <= _mods("import json, ovid2.core")
    assert {"ovid2", "ovid2.core"} <= _mods("from ovid2 import core")
    assert "snowflake.snowpark" in _mods("from snowflake import snowpark")
    assert "pyspark.sql" in _mods("import pyspark.sql as ps")


@pytest.mark.unit
def test_relative_imports_resolve() -> None:
    assert "ovid2.web.services.x" in _mods("from .services import x", "ovid2.web")
    assert "ovid2.core" in _mods("from .. import core", "ovid2.web")


@pytest.mark.unit
def test_type_checking_flag() -> None:
    src = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import pyspark\nimport os\n"
    refs = {r.module: r.type_checking for r in iter_imports(ast.parse(src))}
    assert refs["pyspark"] is True and refs["os"] is False


@pytest.mark.unit
def test_dynamic_imports() -> None:
    tree = ast.parse(
        "import importlib\n"
        "from importlib import import_module as im\n"
        "importlib.import_module('pyspark.sql')\n"
        "im('ovid2.core')\n"
        "__import__('pyspark')\n"
        "importlib.import_module(name)\n"
    )
    mods = sorted((d.module or "<dynamic>") for d in dynamic_imports(tree))
    assert mods == ["<dynamic>", "ovid2.core", "pyspark", "pyspark.sql"]


@pytest.mark.unit
def test_alias_resolution() -> None:
    tree = ast.parse(
        "import subprocess as sp\nfrom subprocess import run\nsp.Popen(x)\nrun(y)\nos.system(z)\n"
    )
    aliases = alias_map(tree)
    calls = [resolved_call_name(n, aliases) for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert {"subprocess.Popen", "subprocess.run", "os.system"} <= set(calls)


@pytest.mark.unit
def test_getattr_literals() -> None:
    assert getattr_literals(ast.parse("getattr(df, 'collect')()")) == [("collect", 1)]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
