"""Contract: the Data Space explorer's trust + import boundaries.

The table detail / sample / count / export features read live data, so
they must keep the harness / API / executor split
(the adr-harness-api-executor-trust-model design note):

1. **The web tier never opens a store or renders a document itself.**
   No module under ``src/ovid2/web/`` may import the export writers
   (``ovid2.exports``), the tabular readers (``ovid2.drivers._tabular``),
   the table-ops executor (``ovid2.tools.table_ops``), a driver's
   introspection module, or a heavy data / document library
   (``pyarrow``, ``pandas``, ``openpyxl``, ``xlsxwriter``, ``docx``,
   ``reportlab``). Everything goes through ``_broker.run_tool_json``.
2. **Every browsable driver can describe what it lists.** A driver that
   implements ``list_dir`` must also override ``describe_table`` and
   ``sample_rows`` — otherwise the explorer shows tables it cannot open.
3. **Every leaf a driver lists is a valid Data Space path.** The table
   page is ``/dataspace/<account>/<listing path>``; a listing path
   containing a character outside the :class:`OdsPath` grammar
   (historically Access's ``file::TABLE``) would be unreachable.
4. **The export job kind is job-only.** It is never offered as a
   pipeline kind (covered in ``test_run_kind_registry_complete.py``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

#: Modules the web tier must never import (prefix match).
_FORBIDDEN_IN_WEB: tuple[str, ...] = (
    "ovid2.exports",
    "ovid2.drivers._tabular",
    "ovid2.tools.table_ops",
    "ovid2.drivers.snowflake.introspect",
    "ovid2.drivers.access.reader",
    "pyarrow",
    "pandas",
    "openpyxl",
    "xlsxwriter",
    "docx",
    "reportlab",
)

#: Drivers whose ``list_dir`` is implemented must override these.
_INTROSPECTION_HOOKS: tuple[str, ...] = ("describe_table", "sample_rows")


def _imports(py: Path) -> list[tuple[int, str]]:
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append((node.lineno, node.module))
            # ``from ovid2.drivers import _tabular`` style.
            out.extend((node.lineno, f"{node.module}.{alias.name}") for alias in node.names)
    return out


def test_web_tier_does_not_import_data_or_document_libraries(src_root: Path) -> None:
    """``src/ovid2/web/**`` never imports readers, writers or their libraries."""
    offenders: list[str] = []
    for py in sorted((src_root / "web").rglob("*.py")):
        for lineno, target in _imports(py):
            if any(target == f or target.startswith(f + ".") for f in _FORBIDDEN_IN_WEB):
                offenders.append(f"{py.relative_to(src_root)}:{lineno} imports {target}")
    assert not offenders, (
        "The web tier must broker table reads / exports to the executor "
        "(ovid2 tools describe-table / sample-table / count-rows / export-table):\n"
        + "\n".join(offenders)
    )


def test_browsable_drivers_implement_table_introspection() -> None:
    """A driver with ``list_dir`` also implements describe_table + sample_rows."""
    from ovid2.drivers.base import Driver
    from ovid2.drivers.registry import DRIVERS, load_class

    offenders: list[str] = []
    for type_name in sorted(DRIVERS):
        cls = load_class(type_name)
        if cls.list_dir is Driver.list_dir:
            continue
        for hook in _INTROSPECTION_HOOKS:
            if getattr(cls, hook) is getattr(Driver, hook):
                offenders.append(f"{type_name}: lists tables but does not implement {hook}()")
    assert not offenders, "\n".join(offenders)


def test_disc_listing_paths_are_valid_dataspace_paths(tmp_path: Path) -> None:
    """Every disc listing path round-trips through :class:`OdsPath`."""
    from ovid2.dataspace.path import OdsPath
    from ovid2.drivers.disc.driver import DiscDriver

    (tmp_path / "landing" / "2026-03").mkdir(parents=True)
    (tmp_path / "landing" / "2026-03" / "DEMOGRAPHIC.csv").write_text("a\n1\n")
    driver = DiscDriver(args={"host_path": str(tmp_path)})
    for prefix in (None, "landing", "landing/2026-03"):
        for entry in driver.list_dir(prefix):
            OdsPath.parse(f"/acct/{entry.path}")


def test_access_table_paths_are_valid_dataspace_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Access tables list as ``<file>/<TABLE>`` (no ``::``) so the table page is reachable."""
    from ovid2.dataspace.path import OdsPath
    from ovid2.drivers.access.driver import AccessDriver

    (tmp_path / "drugs.accdb").write_bytes(b"")
    driver = AccessDriver(args={"host_path": str(tmp_path)})
    monkeypatch.setattr(driver, "_enumerate_tables", lambda *_a, **_k: ["NDC", "Drug_Name"])
    entries = driver.list_dir("drugs.accdb")
    assert [e.path for e in entries] == ["drugs.accdb/NDC", "drugs.accdb/Drug_Name"]
    for entry in entries:
        OdsPath.parse(f"/acct/{entry.path}")
