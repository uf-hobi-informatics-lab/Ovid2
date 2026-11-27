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
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ovid2.core.cancellation import raise_if_cancelled

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
class FixProgress:
    """One legacy fix that finished on a table (for ``fix.*`` events).

    Attributes:
        table: CDM table the fix ran on (``multi_tables`` for
            ``multi_tables_fixer.py`` entries).
        entry: The ``fixes`` dict entry.
        index: 1-based position in the table's ``fixes`` dict.
        total: Number of entries in the table's ``fixes`` dict.
        error: ``None`` when the fix ran; the failure message otherwise.
        skipped: The fix was skipped (a missing optional reference — the
            legacy orange warning); ``error`` holds the reason.
    """

    table: str
    entry: LegacyFixEntry
    index: int
    total: int
    error: str | None = None
    skipped: bool = False


#: Callback the fix stage reports each finished fix to.
FixCallback = Callable[[FixProgress], None]

#: The callback of the fix stage that is running (``None`` = not reporting).
#: Module state because the vendored ``fix_chain`` calls back into the shim
#: (``cf.apply_fix``), not into this module.
_REPORT: dict[str, Any] = {"cb": None, "table": "", "fixes": ()}


def _report(progress: FixProgress) -> None:
    cb = _REPORT["cb"]
    if cb is None:
        return
    try:
        cb(progress)
    except Exception:  # a display hook must never break the fix stage
        logger.debug("fix progress callback failed", exc_info=True)


def _position(entry: LegacyFixEntry) -> tuple[int, int]:
    """Return ``(index, total)`` of ``entry`` in the table being fixed.

    Searches after the last reported position first, so a fix listed twice
    in one ``fixes`` dict gets its own index each time.
    """
    fixes: tuple[LegacyFixEntry, ...] = _REPORT["fixes"]
    last = int(_REPORT.get("last", 0))
    order = list(range(last, len(fixes))) + list(range(0, last))
    for i in order:
        if fixes[i] == entry:
            _REPORT["last"] = i + 1
            return i + 1, len(fixes)
    return 0, len(fixes)


@contextlib.contextmanager
def _reporting(cb: FixCallback | None, table: str, fixes: list[LegacyFixEntry]) -> Iterator[None]:
    saved = dict(_REPORT)
    _REPORT.clear()
    _REPORT.update(cb=cb, table=table, fixes=tuple(fixes), last=0)
    try:
        yield
    finally:
        _REPORT.clear()
        _REPORT.update(saved)


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


def vendor_partner(pipeline: str) -> str:
    """Return the vendored legacy partner folder for ``pipeline``.

    A pipeline cloned from a ``legacy_ovid`` port records the template in
    ``LEGACY_VENDOR_PARTNER`` (set by ``ovid2 tools new-pipeline``); the
    vendored fixer scripts live under that name. Defaults to the
    pipeline's own name.
    """
    from ovid2.legacy.profile import pipeline_setting

    return str(pipeline_setting(pipeline, "LEGACY_VENDOR_PARTNER", None) or pipeline)


def fixer_tables(partner: str) -> list[str]:
    """Return the tables that have a legacy ``<t>_fixer.py`` (excl. multi_tables)."""
    folder = vendor_root() / "partners" / vendor_partner(partner) / "fixer_scripts"
    return sorted(
        p.name[: -len("_fixer.py")]
        for p in folder.glob("*_fixer.py")
        if p.name != "multi_tables_fixer.py"
    )


def _session_is_spark(spark: Any) -> bool:
    from ovid2.legacy.fix_shim import NonStoppingSession

    target = (
        object.__getattribute__(spark, "_spark") if isinstance(spark, NonStoppingSession) else spark
    )
    return type(target).__module__.startswith("pyspark.")


def _pickle_vendor_by_value(common: Path) -> None:
    """Import the vendored helper modules and register them pickle-by-value (Spark only)."""
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
    spark_family = _session_is_spark(spark)
    overlay: contextlib.AbstractContextManager[None] = contextlib.nullcontext()
    if not spark_family:
        # Vendored code imports ``pyspark`` by name: bind it to the
        # engine-portable package for the duration of the fix stage.
        from ovid2.core.engines.sparkapi import overlay as _overlay

        overlay = _overlay()
    try:
        overlay.__enter__()
        fix_shim.install(spark, partner=partner, folder_label=folder_label, work_root=work_root)
        sys.path.insert(0, str(vendor / "common"))
        for name in ("fix_chain", "fix_transforms"):
            sys.modules.pop(name, None)
        if spark_family:
            # Legacy module-level UDFs (``fix_transforms.shorten_range_value_udf``
            # …) would be pickled *by reference* — Python workers cannot
            # import the vendored modules (or the in-memory shim modules
            # they import). Pickle them by value instead.
            _pickle_vendor_by_value(vendor / "common")
        yield
    finally:
        overlay.__exit__(None, None, None)
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
            vendor
            / "partners"
            / vendor_partner(partner)
            / "custom_fixes"
            / entry.fix_name
            / f"{entry.version}.py"
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
    flush_chain_fix()
    fix_shim.reset_failures()
    outer = _REPORT.get("in_script", False)
    _REPORT["in_script"] = True
    try:
        with contextlib.suppress(SystemExit):
            runpy.run_path(str(script), run_name="__main__")
    except Exception as exc:
        _report_entry(entry, error=str(exc) or type(exc).__name__)
        raise
    finally:
        _REPORT["in_script"] = outer
        sys.argv = saved_argv
    fatal = fix_shim.fatal_failures()
    if fatal:
        fix_shim.quarantine(output_path, fixed_table_name)
        _report_entry(entry, error=fatal[0])
        raise LegacyFixError(
            f"Fix {entry.fix_type}/{entry.fix_name}/{entry.version} failed on "
            f"{fixed_table_name}: {fatal[0]}"
        )
    warnings = fix_shim.recorded_failures()
    if warnings:
        # Legacy orange warning (a missing optional reference): the fix
        # ran but changed nothing.
        _report_entry(entry, error=warnings[0], skipped=True)
    else:
        _report_entry(entry)


def _report_entry(
    entry: LegacyFixEntry,
    *,
    error: str | None = None,
    skipped: bool = False,
    index: int | None = None,
) -> None:
    if index is None:
        index, total = _position(entry)
    else:
        total = len(_REPORT["fixes"])
        _REPORT["last"] = index
    _report(
        FixProgress(
            table=str(_REPORT["table"]),
            entry=entry,
            index=index,
            total=total,
            error=error,
            skipped=skipped,
        )
    )


# -- in-process chain fixes ---------------------------------------------------
#
# The vendored ``fix_chain.run_fix_chain`` runs chain-supported fixes as
# lazy DataFrame transforms and announces each through
# ``cf.print_fixer_status(current_count=<n>, …)`` *before* applying it; a
# missing optional reference is reported through ``cf.print_failure_message``.
# The shim forwards both here; a fix is reported when the next one starts
# (or the chain ends), so a skip / failure is attributed to the right fix.


def chain_fix_started(index: Any) -> None:
    """``print_fixer_status`` from the chain: fix ``index`` (1-based) begins."""
    if _REPORT.get("in_script"):
        return  # a fix script's own status line, reported by run_fix_script
    flush_chain_fix()
    if _REPORT["cb"] is None or not isinstance(index, int):
        return
    fixes: tuple[LegacyFixEntry, ...] = _REPORT["fixes"]
    if 1 <= index <= len(fixes):
        _REPORT["pending"] = (fixes[index - 1], index, None)


def chain_fix_skipped(reason: str) -> None:
    """``print_failure_message`` while a chain fix is pending: mark it skipped."""
    pending = _REPORT.get("pending")
    if pending is not None and not _REPORT.get("in_script"):
        _REPORT["pending"] = (pending[0], pending[1], str(reason))


def flush_chain_fix(error: str | None = None) -> None:
    """Report the pending chain fix (applied / skipped / ``error`` = failed)."""
    pending = _REPORT.pop("pending", None)
    if pending is None:
        return
    entry, index, skip_reason = pending
    if error is not None:
        _report_entry(entry, error=error, index=index)
    elif skip_reason is not None:
        _report_entry(entry, error=skip_reason, skipped=True, index=index)
    else:
        _report_entry(entry, index=index)


def run_table_fixes(
    spark: SparkSession,
    *,
    partner: str,
    table: str,
    work_root: str,
    folder_label: str,
    on_fix: FixCallback | None = None,
) -> None:
    """Run legacy ``<table>_fixer.py`` for one table.

    Args:
        spark: Active session (shared with the shimmed legacy code).
        partner: Legacy partner name (= Ovid2 pipeline).
        table: Table name.
        work_root: Scratch root holding ``mapper_output/`` and ``fixer_output/``.
        folder_label: Legacy ``input_data_folder`` (messages only).
        on_fix: Optional callback, called once per fix of the table's
            ``fixes`` dict when it finishes (applied, skipped or failed).
    """
    script = (
        vendor_root() / "partners" / vendor_partner(partner) / "fixer_scripts" / f"{table}_fixer.py"
    )
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

    with (
        _legacy_import_env(spark, partner, folder_label, work_root),
        _reporting(on_fix, table, fixes),
    ):
        if uses_chain:
            # Legacy ``fix_chain.run_partner_fixes`` with explicit paths
            # (its own path builder hard-codes ``/app/partners/...``).
            import fix_chain  # type: ignore[import-not-found]

            from ovid2.legacy import fix_shim

            cf = fix_shim.CommonFuncitons(partner.upper())
            try:
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
            except Exception as exc:
                flush_chain_fix(error=str(exc) or type(exc).__name__)
                raise
            flush_chain_fix()
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


#: ``FixProgress.table`` of ``multi_tables_fixer.py`` entries.
MULTI_TABLES = "multi_tables"


def run_multi_table_fixes(
    spark: SparkSession,
    *,
    partner: str,
    work_root: str,
    folder_label: str,
    on_fix: FixCallback | None = None,
) -> None:
    """Run legacy ``multi_tables_fixer.py`` (``-i/-o fixer_output/``)."""
    script = (
        vendor_root()
        / "partners"
        / vendor_partner(partner)
        / "fixer_scripts"
        / "multi_tables_fixer.py"
    )
    if not script.is_file():
        return
    fixer_out = os.path.join(work_root, "fixer_output") + "/"
    entries = read_fixes(script)
    with (
        _legacy_import_env(spark, partner, folder_label, work_root),
        _reporting(on_fix, MULTI_TABLES, entries),
    ):
        for entry in entries:
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
    on_table: Callable[[str, str], None] | None = None,
    on_fix: FixCallback | None = None,
) -> dict[str, str]:
    """Run the whole legacy fix stage and return per-table failures.

    Tables are fixed in legacy order (``os.listdir`` order of
    ``fixer_scripts`` is not stable, but each table fix only reads the
    *mapped* files of other tables, so order between tables does not
    change results), then the multi-table fixes.

    Args:
        on_table: Optional ``(table, state)`` callback — called with
            ``"active"`` before a table's fixes run and ``"waiting"`` after
            (success or failure), so the caller can time each table's fix.
        on_fix: Optional per-fix callback (:class:`FixProgress`) — the
            caller turns it into ``fix.applied`` / ``fix.skipped`` /
            ``fix.failed`` events.

    Returns:
        ``{table: error message}`` for tables whose fixes failed.
    """
    failures: dict[str, str] = {}
    for table in tables:
        raise_if_cancelled()
        started = time.monotonic()
        if on_table is not None:
            on_table(table, "active")
        try:
            run_table_fixes(
                spark,
                partner=partner,
                table=table,
                work_root=work_root,
                folder_label=folder_label,
                on_fix=on_fix,
            )
            logger.info("legacy fixes for %s took %.1fs", table, time.monotonic() - started)
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.exception("legacy fix stage failed for %s", table)
            failures[table] = str(exc)
        finally:
            if on_table is not None:
                on_table(table, "waiting")
    try:
        run_multi_table_fixes(
            spark, partner=partner, work_root=work_root, folder_label=folder_label, on_fix=on_fix
        )
    except Exception as exc:
        raise_if_cancelled(exc)
        logger.exception("legacy multi_tables_fixer failed")
        failures["multi_tables"] = str(exc)
    return failures
