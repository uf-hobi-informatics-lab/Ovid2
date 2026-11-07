"""Contract: ``legacy_ovid`` ports are complete, loadable and contained.

The legacy-parity port (``ETL_PROFILE = "legacy_ovid"``) deliberately keeps
legacy-Ovid code verbatim — including eager actions and ``.rdd`` — so it
must stay *contained*:

1. Every pipeline declaring the profile is generated from a legacy
   partner, has a ``formatters_custom`` port for every legacy formatter
   that produces output, a byte-identical ``dictionaries.py`` (or a
   reported repair), and vendored fixer scripts.
2. Every generated port module imports and exposes the class
   :func:`ovid2.core.loaders.load_formatter` looks up, subclassing
   :class:`ovid2.legacy.formatter.LegacyFormatter`.
3. Ports never contain a legacy absolute path (``/app/…``, ``/data/…``)
   — every legacy path is rewritten to ``rt.*``.
4. Eager-action / ``.rdd`` waivers (``ovid2: legacy-port``) only appear in
   generated ports under ``pipelines/ovid2_pipelines/*/formatters_custom/``,
   never in ``src/``.
5. ``legacy_ovid`` pipelines use no native ``FIXER_CHAINS`` (the vendored
   legacy fix stage is the only fixer).
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PIPES = _ROOT / "pipelines" / "ovid2_pipelines"
_VENDOR = _ROOT / "pipelines" / "legacy_ovid_vendor"
_MARKER = "ovid2: legacy-port"
_LEGACY_PATH = re.compile(r"""['"]/(app|data|ovid_credentials)/""")


def _legacy_pipelines() -> list[str]:
    out: list[str] = []
    for settings in sorted(_PIPES.glob("*/pipeline_settings.py")):
        if re.search(
            r'^ETL_PROFILE\s*=\s*"legacy_ovid"', settings.read_text(encoding="utf-8"), re.M
        ):
            out.append(settings.parent.name)
    return out


_PIPELINES = _legacy_pipelines()


@pytest.mark.contract
def test_some_legacy_pipelines_exist() -> None:
    if not _VENDOR.is_dir():
        pytest.skip("legacy vendor tree absent (public mirror: site ports are excluded)")
    assert _PIPELINES, "no legacy_ovid pipelines found — generator not run?"


@pytest.mark.contract
@pytest.mark.parametrize("pipeline", _PIPELINES)
def test_legacy_pipeline_is_complete(pipeline: str) -> None:
    pkg = _PIPES / pipeline
    assert (pkg / "dictionaries.py").is_file()
    assert (_VENDOR / "partners" / pipeline / "fixer_scripts").is_dir(), (
        "fixer scripts not vendored"
    )
    ports = sorted((pkg / "formatters_custom").glob("*_formatter.py"))
    assert ports, f"{pipeline}: no formatter ports"
    chains_src = (pkg / "fixer_configs.py").read_text(encoding="utf-8")
    tree = ast.parse(chains_src)
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(target, ast.Name) and target.id in ("FIXER_CHAINS", "MULTI_TABLE_FIXES"):
                assert ast.literal_eval(node.value) in ({}, []), (  # type: ignore[arg-type]
                    f"{pipeline}: legacy_ovid pipelines must not declare native {target.id}"
                )


@pytest.mark.contract
@pytest.mark.parametrize("pipeline", _PIPELINES)
# Verbatim legacy code: typed ``udf(...)`` defs and ``"\%"`` escapes warn on
# import under Python 3.14 / PySpark 4 — inherent to the verbatim port.
@pytest.mark.filterwarnings("ignore::UserWarning")
@pytest.mark.filterwarnings("ignore::SyntaxWarning")
def test_legacy_ports_load(pipeline: str) -> None:
    pytest.importorskip("pyspark")
    from ovid2.legacy.formatter import LegacyFormatter

    for port in sorted((_PIPES / pipeline / "formatters_custom").glob("*_formatter.py")):
        table = port.name[: -len("_formatter.py")]
        module = importlib.import_module(
            f"ovid2_pipelines.{pipeline}.formatters_custom.{table}_formatter"
        )
        cls = getattr(module, f"{table.capitalize()}Formatter", None)
        assert cls is not None, f"{port}: missing {table.capitalize()}Formatter"
        assert issubclass(cls, LegacyFormatter), f"{port}: not a LegacyFormatter"
        assert table == cls.TABLE and f"formatted_{table}.csv" == cls.OUTPUT_FILE
        text = port.read_text(encoding="utf-8")
        assert _MARKER in text, f"{port}: missing generated legacy-port marker"
        assert not _LEGACY_PATH.search(text), f"{port}: unrewritten legacy path literal"


@pytest.mark.contract
def test_legacy_port_waiver_only_in_generated_ports() -> None:
    offenders: list[str] = []
    for py in (_ROOT / "src" / "ovid2").rglob("*.py"):
        head = py.read_text(encoding="utf-8")[:3000]
        if _MARKER in head and "tools/legacy_formatter_migrator.py" not in str(py):
            offenders.append(str(py.relative_to(_ROOT)))
    for py in _PIPES.rglob("*.py"):
        if "_superseded" in py.parts:
            continue
        head = py.read_text(encoding="utf-8", errors="ignore")[:3000]
        if _MARKER in head and py.parent.name != "formatters_custom":
            offenders.append(str(py.relative_to(_ROOT)))
    assert not offenders, "legacy-port waiver outside generated ports:\n" + "\n".join(offenders)


@pytest.mark.contract
def test_superseded_never_imported() -> None:
    """``_superseded`` holds pre-port references only — not a package path."""
    for sup in _PIPES.glob("*/_superseded"):
        assert not (sup / "__init__.py").exists(), f"{sup} must not be an importable package"
