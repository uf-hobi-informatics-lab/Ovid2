"""Legacy-Ovid fix stage, executed verbatim (``legacy_ovid`` profile).

Legacy ran fixes per table as ``partners/<p>/fixer_scripts/<t>_fixer.py``:

1. ``cp mapper_output/mapped_<t>.csv fixer_output/fixed_<t>.csv`` (when the
   mapped file exists), then
2. for each entry of the script's ``fixes`` dict, in key order, the fix
   script ``common/common_fixes/<fix>/<ver>.py`` (or the partner
   ``custom_fixes``) with ``-i mapper_output/ -o fixer_output/ -t
   mapped_<t>.csv -ftm fixed_<t>.csv -sr1 … -sr2 …`` — under
   ``FIX_CHAIN_MODE=True`` the 1.0 fixes known to ``fix_transforms`` run
   in-process in one chain, the rest as subprocesses;
3. after every table, ``multi_tables_fixer.py`` (``-i/-o fixer_output/``).

This module reproduces that *with the vendored legacy code itself*
(``pipelines/legacy_ovid_vendor/``): the per-table ``fixes`` dicts are read
from the vendored fixer scripts with ``ast`` (never executed), the vendored
``fix_chain.run_fix_chain`` runs chain-supported fixes, and every other fix
script is executed in-process via ``runpy`` with the legacy ``argv`` — on
the same ``mapped_*.csv`` / ``fixed_*.csv`` files, in a per-run scratch
directory. The legacy ``commonFunctions`` / ``settings`` / ``ovid_logging``
modules are provided by :mod:`ovid2.legacy.fix_shim` so the scripts see the
Ovid2 Spark session and write through the legacy single-file CSV writer.

Ovid2 orchestration for a ``legacy_ovid`` run therefore becomes two-phase:
format → dedup → map for **every** table (each writes ``mapped_<t>.csv`` to
the scratch dir), then this fix stage, then the ``fixed_<t>.csv`` files are
read back and written to the output address.
"""

from __future__ import annotations

import ast
import contextlib
import logging
import os
import runpy
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

logger = logging.getLogger(__name__)


def vendor_root() -> Path:
    """Return the vendored legacy tree (``pipelines/legacy_ovid_vendor``).

    Resolved from ``$OVID2_LEGACY_VENDOR_DIR`` or relative to the
    ``ovid2_pipelines`` namespace package (``pipelines/``).
    """
    override = os.environ.get("OVID2_LEGACY_VENDOR_DIR")
    if override:
        return Path(override)
    import ovid2_pipelines

    for base in getattr(ovid2_pipelines, "__path__", []):
        candidate = Path(base).parent / "legacy_ovid_vendor"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "legacy_ovid_vendor not found next to ovid2_pipelines; set OVID2_LEGACY_VENDOR_DIR."
    )


class LegacyFixError(RuntimeError):
    """Raised when a legacy fix fails (legacy quarantined the table)."""


@dataclass(frozen=True)
class LegacyFixEntry:
    """One entry of a legacy ``fixes`` dict."""

    fix_type: str
    fix_name: str
    version: str
    src1: str
    src2: str

    def as_dict(self) -> dict[str, str]:
        """Return the legacy dict shape."""
        return {
            "fix_type": self.fix_type,
            "fix_name": self.fix_name,
            "version": self.version,
            "src1": self.src1,
            "src2": self.src2,
        }


def read_fixes(script: Path) -> list[LegacyFixEntry]:
    """Return the ``fixes`` dict of a legacy fixer script, in key order.

    The script is parsed, never executed. Commented-out entries are, as in
    legacy, simply absent from the literal.
    """
    tree = ast.parse(script.read_text(encoding="utf-8"))
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and target.id == "fixes" and value is not None:
            raw: dict[str, dict[str, str]] = ast.literal_eval(value)
            ordered = sorted(raw, key=lambda k: int(k))
            return [
                LegacyFixEntry(
                    fix_type=str(raw[k].get("fix_type", "common")),
                    fix_name=str(raw[k]["fix_name"]),
                    version=str(raw[k].get("version", "1.0")),
                    src1=str(raw[k].get("src1", "")),
                    src2=str(raw[k].get("src2", "")),
                )
                for k in ordered
            ]
    return []


def fixer_tables(partner: str) -> list[str]:
    """Return the tables that have a legacy ``<t>_fixer.py`` (excl. multi_tables)."""
    folder = vendor_root() / "partners" / partner / "fixer_scripts"
    return sorted(
        p.name[: -len("_fixer.py")]
        for p in folder.glob("*_fixer.py")
        if p.name != "multi_tables_fixer.py"
    )


def _pickle_vendor_by_value(common: Path) -> None:
    """Import the vendored helper modules and register them pickle-by-value."""
    import importlib

    from pyspark import cloudpickle

    for name in ("fix_transforms", "fix_chain"):
        if not (common / f"{name}.py").exists():
            continue
        module = importlib.import_module(name)
        cloudpickle.register_pickle_by_value(module)  # type: ignore[no-untyped-call]


@contextlib.contextmanager
def _legacy_import_env(
    spark: SparkSession, partner: str, folder_label: str, work_root: str = ""
) -> Iterator[None]:
    """Expose the vendored legacy modules + shim modules to ``import``."""
    from ovid2.legacy import fix_shim

    vendor = vendor_root()
    saved_path = list(sys.path)
    saved_mods = {k: sys.modules.get(k) for k in fix_shim.SHIM_MODULE_NAMES}
    for name in ("fix_chain", "fix_transforms"):
        saved_mods.setdefault(name, sys.modules.get(name))
    try:
        fix_shim.install(spark, partner=partner, folder_label=folder_label, work_root=work_root)
        sys.path.insert(0, str(vendor / "common"))
        for name in ("fix_chain", "fix_transforms"):
            sys.modules.pop(name, None)
        # Legacy module-level UDFs (``fix_transforms.shorten_range_value_udf``
        # …) would be pickled *by reference* — Python workers cannot import
        # the vendored modules (or the in-memory shim modules they import).
        # Pickle them by value instead.
        _pickle_vendor_by_value(vendor / "common")
        yield
    finally:
        fix_shim.uninstall()
        sys.path[:] = saved_path
        for name, mod in saved_mods.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


def _script_path(entry: LegacyFixEntry, partner: str) -> Path:
    vendor = vendor_root()
    if entry.fix_type == "custom":
        return (
            vendor / "partners" / partner / "custom_fixes" / entry.fix_name / f"{entry.version}.py"
        )
    return vendor / "common" / "common_fixes" / entry.fix_name / f"{entry.version}.py"


def run_fix_script(
    entry: LegacyFixEntry,
    *,
    partner: str,
    folder_label: str,
    mapped_table_name: str,
    fixed_table_name: str,
    input_path: str,
    output_path: str,
) -> None:
    """Execute one legacy fix script in-process (legacy ``cf.apply_fix``).

    Raises:
        LegacyFixError: When the script reports a fatal failure (legacy
            quarantined the fixed table and raised).
    """
    from ovid2.legacy import fix_shim

    script = _script_path(entry, partner)
    if not script.is_file():
        # Legacy: ``python <missing>.py`` exits 2 → apply_fix quarantines
        # ``fixed_<t>.csv`` as ``FAILED_…`` and raises.
        fix_shim.quarantine(output_path, fixed_table_name)
        raise LegacyFixError(
            f"Fix {entry.fix_type}/{entry.fix_name}/{entry.version} failed on "
            f"{fixed_table_name}: script does not exist ({script.name})"
        )
    argv = [
        str(script),
        "-f",
        folder_label,
        "-t",
        mapped_table_name,
        "-p",
        partner,
        "-ftm",
        fixed_table_name,
        "-i",
        input_path,
        "-o",
        output_path,
        "-sr1",
        entry.src1,
        "-sr2",
        entry.src2,
    ]
    saved_argv = sys.argv
    sys.argv = argv
    fix_shim.reset_failures()
    try:
        with contextlib.suppress(SystemExit):
            runpy.run_path(str(script), run_name="__main__")
    finally:
        sys.argv = saved_argv
    fatal = fix_shim.fatal_failures()
    if fatal:
        fix_shim.quarantine(output_path, fixed_table_name)
        raise LegacyFixError(
            f"Fix {entry.fix_type}/{entry.fix_name}/{entry.version} failed on "
            f"{fixed_table_name}: {fatal[0]}"
        )


def run_table_fixes(
    spark: SparkSession,
    *,
    partner: str,
    table: str,
    work_root: str,
    folder_label: str,
) -> None:
    """Run legacy ``<table>_fixer.py`` for one table.

    Args:
        spark: Active session (shared with the shimmed legacy code).
        partner: Legacy partner name (= Ovid2 pipeline).
        table: Table name.
        work_root: Scratch root holding ``mapper_output/`` and ``fixer_output/``.
        folder_label: Legacy ``input_data_folder`` (messages only).
    """
    script = vendor_root() / "partners" / partner / "fixer_scripts" / f"{table}_fixer.py"
    mapper_out = os.path.join(work_root, "mapper_output") + "/"
    fixer_out = os.path.join(work_root, "fixer_output") + "/"
    mapped = f"mapped_{table}.csv"
    fixed = f"fixed_{table}.csv"
    os.makedirs(fixer_out, exist_ok=True)
    if not os.path.exists(mapper_out + mapped):
        logger.warning("%s%s: No such file or directory", mapper_out, mapped)
        return
    fixes = read_fixes(script) if script.is_file() else []
    uses_chain = script.is_file() and "run_partner_fixes" in script.read_text(encoding="utf-8")

    with _legacy_import_env(spark, partner, folder_label, work_root):
        if uses_chain:
            # Legacy ``fix_chain.run_partner_fixes`` with explicit paths
            # (its own path builder hard-codes ``/app/partners/...``).
            import fix_chain  # type: ignore[import-not-found]

            from ovid2.legacy import fix_shim

            cf = fix_shim.CommonFuncitons(partner.upper())
            fix_chain.run_fix_chain(
                cf,
                spark,
                partner,
                folder_label,
                mapped,
                fixed,
                mapper_out,
                fixer_out,
                [e.as_dict() for e in fixes],
            )
            return
        # Legacy per-script loop: cp mapped → fixed, then apply each fix.
        import shutil

        shutil.copy2(mapper_out + mapped, fixer_out + fixed)
        for entry in fixes:
            run_fix_script(
                entry,
                partner=partner,
                folder_label=folder_label,
                mapped_table_name=mapped,
                fixed_table_name=fixed,
                input_path=mapper_out,
                output_path=fixer_out,
            )


def run_multi_table_fixes(
    spark: SparkSession, *, partner: str, work_root: str, folder_label: str
) -> None:
    """Run legacy ``multi_tables_fixer.py`` (``-i/-o fixer_output/``)."""
    script = vendor_root() / "partners" / partner / "fixer_scripts" / "multi_tables_fixer.py"
    if not script.is_file():
        return
    fixer_out = os.path.join(work_root, "fixer_output") + "/"
    with _legacy_import_env(spark, partner, folder_label, work_root):
        for entry in read_fixes(script):
            run_fix_script(
                entry,
                partner=partner,
                folder_label=folder_label,
                mapped_table_name="notUsed",
                fixed_table_name="notUsed",
                input_path=fixer_out,
                output_path=fixer_out,
            )


def run_fix_stage(
    spark: SparkSession,
    *,
    partner: str,
    tables: list[str],
    work_root: str,
    folder_label: str,
) -> dict[str, str]:
    """Run the whole legacy fix stage and return per-table failures.

    Tables are fixed in legacy order (``os.listdir`` order of
    ``fixer_scripts`` is not stable, but each table fix only reads the
    *mapped* files of other tables, so order between tables does not
    change results), then the multi-table fixes.

    Returns:
        ``{table: error message}`` for tables whose fixes failed.
    """
    failures: dict[str, str] = {}
    for table in tables:
        try:
            run_table_fixes(
                spark, partner=partner, table=table, work_root=work_root, folder_label=folder_label
            )
        except Exception as exc:
            logger.exception("legacy fix stage failed for %s", table)
            failures[table] = str(exc)
    try:
        run_multi_table_fixes(
            spark, partner=partner, work_root=work_root, folder_label=folder_label
        )
    except Exception as exc:
        logger.exception("legacy multi_tables_fixer failed")
        failures["multi_tables"] = str(exc)
    return failures
