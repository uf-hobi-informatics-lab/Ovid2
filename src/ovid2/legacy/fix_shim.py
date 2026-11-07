"""In-process stand-ins for the legacy ``common/`` modules the fix code imports.

The vendored legacy fix code (``pipelines/legacy_ovid_vendor/common/``)
does ``from commonFunctions import CommonFuncitons``, ``from settings import
…`` and ``from ovid_logging import get_logger``. :func:`install` registers
module objects under those names whose attributes are faithful ports of
the legacy behaviour the fixes rely on:

* ``CommonFuncitons.get_spark_session`` → the Ovid2 session (legacy built
  one per subprocess with ``getOrCreate``);
* ``spark_read`` / ``write_pyspark_output_file`` /
  ``append_pyspark_output_file`` / ``copy_data`` → identical reader options
  and the identical single-file writer (header from schema, sorted parts,
  ``NullType`` → string, quoting off, ``nullValue=None``);
* ``apply_fix`` → in-process :func:`ovid2.legacy.fix_stage.run_fix_script`;
* ``print_failure_message`` → records failures; "Path does not exist"
  messages are warnings, exactly as legacy's ``_count_fatal_failure_messages``;
* ``read_from_snowflake`` → not available in parity runs (raises; the only
  caller, ``append_to_existing_lds_address_hx_fix``, then reports the
  failure and leaves the table unchanged — the same as a legacy run that
  cannot reach Snowflake).

Hard-coded legacy paths inside the vendored code (``/app/common/lists``,
``/app/common/cdm``) are redirected by :func:`resolve_legacy_path`.
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
import sys
import types
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession

logger = logging.getLogger("ovid2.legacy.fixes")

#: Unpatched filesystem probes (the path hooks below wrap the public ones).
_REAL_EXISTS = os.path.exists
_REAL_ISFILE = os.path.isfile

SHIM_MODULE_NAMES: tuple[str, ...] = ("commonFunctions", "settings", "ovid_logging")

#: Legacy ``settings`` values the fix code imports (verbatim).
LEGACY_SETTINGS: dict[str, Any] = {
    "FIX_CHAIN_MODE": True,
    "FIX_CHAIN_CHECKPOINT_FORMAT": "parquet",
    "FIX_CHAIN_CHECKPOINT_COMPRESSION": "zstd",
    "PATTERNS_REMOVER_APPLY_PATTERNS": False,
    "HARVEST_CDM_VERSION": "070",
    "MGMT_FIELDS_VALUE": "01",
    "PARTIAL_OUTPUT_SUFFIX": ".partial",
}

_STATE: dict[str, Any] = {
    "spark": None,
    "partner": "",
    "folder": "",
    "work_root": "",
    "failures": [],
}


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _vendor_common() -> str:
    from ovid2.legacy.fix_stage import vendor_root

    return str(vendor_root() / "common")


def resolve_legacy_path(path: str) -> str:
    """Redirect a legacy in-container path to its Ovid2 location.

    ``/app/common/lists/<f>`` and relative ``common/lists/<f>`` → vendored
    lists, falling back to the material lists dir for large files (e.g.
    the geocoding CSV); ``/app/common/cdm/...`` → vendored CDM files.
    """
    from ovid2.config.settings import COMMON_LISTS_DIR

    # ``/app/partners/<p>/data/<folder>/{mapper,fixer}_output/...`` — the
    # run's legacy scratch directory (``loinc_mover_fix`` 2.0 builds it).
    root = _STATE.get("work_root") or ""
    partners_prefix = "/app/partners/"  # noqa: OVID2002 — legacy literal being redirected
    if root and path.startswith(partners_prefix):
        for stage in ("mapper_output", "fixer_output", "deduplicator_output", "formatter_output"):
            marker = "/" + stage
            if path.endswith(marker):
                return os.path.join(root, stage)
            if marker + "/" in path:
                return os.path.join(root, stage, path.split(marker + "/", 1)[1])

    for prefix in ("/app/common/", "common/"):  # noqa: OVID2002 — legacy literal being redirected
        if path.startswith(prefix):
            rest = path[len(prefix) :]
            vendored = os.path.join(_vendor_common(), rest)
            if _REAL_EXISTS(vendored) or glob.glob(vendored):
                return vendored
            if rest.startswith("lists/"):
                return os.path.join(str(COMMON_LISTS_DIR), rest[len("lists/") :])
            return vendored
    return path


# ---------------------------------------------------------------------------
# Failure bookkeeping (legacy fix failure marker semantics)
# ---------------------------------------------------------------------------


def reset_failures() -> None:
    """Start a new fix: clear recorded failure messages."""
    _STATE["failures"] = []


def fatal_failures() -> list[str]:
    """Return failure messages that legacy counted as fatal."""
    return [
        t
        for t in _STATE["failures"]
        if "Path does not exist" not in t and "does not exist in" not in t
    ]


def quarantine(output_path: str, fixed_table_name: str) -> None:
    """Legacy ``_quarantine_failed_fixed_table``."""
    src = os.path.join(output_path, fixed_table_name)
    if os.path.isfile(src):
        os.replace(src, os.path.join(output_path, f"FAILED_{fixed_table_name}"))


# ---------------------------------------------------------------------------
# Writer (legacy write_pyspark_output_file contract)
# ---------------------------------------------------------------------------


def write_tab_file(df: DataFrame, folder: str, output_file_name: str) -> None:
    """Write ``df`` as one tab CSV exactly like legacy ``write_pyspark_output_file``.

    Writes to a temporary sibling then renames, so a fix that reads and
    rewrites the same ``fixed_<t>.csv`` behaves like legacy (which wrote
    ``<name>.partial`` and ``os.replace``-d it).
    """
    from pyspark.sql import functions as sf
    from pyspark.sql.types import NullType

    out = df.select(
        *[
            sf.lit(None).cast("string").alias(f.name)
            if isinstance(f.dataType, NullType)
            else f.name
            for f in df.schema
        ]
    )
    folder = folder if folder.endswith("/") else folder + "/"
    os.makedirs(folder, exist_ok=True)
    final_path = os.path.join(folder, output_file_name).replace("tmp_", "")
    work = folder + "tmp_" + output_file_name
    partial = final_path + ".partial"
    header = ("\t".join(out.columns) + "\n").encode("utf-8")
    try:
        (
            out.write.format("csv")
            .option("header", False)
            .option("nullValue", None)
            .option("delimiter", "\t")
            .option("quote", "")
            .mode("overwrite")
            .save(work)
        )
        parts = sorted(glob.glob(os.path.join(work, "part-*.csv")))
        with open(partial, "wb") as fh:
            fh.write(header)
            for part in parts:
                with open(part, "rb") as src:
                    shutil.copyfileobj(src, fh)
        os.replace(partial, final_path)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if os.path.exists(partial):
            os.remove(partial)


class NonStoppingSession:
    """A ``SparkSession`` proxy whose :meth:`stop` only stops *this proxy*.

    Legacy scripts stop "their" session; the shared Ovid2 session must live
    on. But in legacy the stop was real: a fix that called ``spark.stop()``
    in an ``except`` arm and then carried on (``loinc_mover_fix`` 2.0 does)
    failed every later Spark call with ``Cannot call methods on a stopped
    SparkContext``. So after :meth:`stop`, any further use of this proxy
    raises the same error; a fresh proxy (the next script) works again.
    """

    def __init__(self, spark: SparkSession) -> None:
        object.__setattr__(self, "_spark", spark)
        object.__setattr__(self, "_stopped", False)

    def stop(self) -> None:
        """Mark this proxy stopped (the underlying session keeps running)."""
        object.__setattr__(self, "_stopped", True)

    def __getattr__(self, name: str) -> Any:
        if object.__getattribute__(self, "_stopped"):
            raise RuntimeError(
                "java.lang.IllegalStateException: Cannot call methods on a stopped SparkContext."
            )
        return getattr(object.__getattribute__(self, "_spark"), name)


# ---------------------------------------------------------------------------
# CommonFuncitons shim
# ---------------------------------------------------------------------------


class CommonFuncitons:
    """Subset of legacy ``CommonFuncitons`` used by fixes and the fix chain."""

    _overall_start_time = None

    def __init__(self, partner: str) -> None:
        from ovid2.legacy.udfs import legacy_udfs

        self.partner = partner
        udfs = legacy_udfs(str(_STATE["partner"] or partner))
        self.format_date_udf = udfs.format_date
        self.format_time_udf = udfs.format_time
        self.get_current_time_udf = udfs.current_time

    # -- session / io ----------------------------------------------------

    @classmethod
    def get_spark_session(cls, app_name: str) -> Any:
        """Return the shared Ovid2 session (legacy: ``getOrCreate``).

        Wrapped so the ``spark.stop()`` every legacy fix script calls on
        exit does not tear down the Ovid2 run's session.
        """
        del app_name
        spark = _STATE["spark"]
        if spark is None:
            raise RuntimeError("legacy fix shim not installed (no Spark session).")
        return NonStoppingSession(spark)

    @classmethod
    def spark_read(cls, file_path: str, spark: SparkSession) -> DataFrame:
        """Legacy ``spark_read``."""
        return spark.read.csv(
            resolve_legacy_path(file_path),
            sep="\t",
            header=True,
            quote='"',
            inferSchema=False,
            multiLine=False,
            escape='"',
        )

    @classmethod
    def write_pyspark_output_file(
        cls, payspark_df: DataFrame, output_file_name: str, output_data_folder_path: str
    ) -> None:
        """Legacy ``write_pyspark_output_file``."""
        write_tab_file(payspark_df, resolve_legacy_path(output_data_folder_path), output_file_name)

    @classmethod
    def append_pyspark_output_file(
        cls, pyspark_df: DataFrame, output_file_name: str, output_data_folder_path: str
    ) -> None:
        """Legacy ``append_pyspark_output_file`` (append body rows)."""
        output_data_folder_path = resolve_legacy_path(output_data_folder_path)
        target = os.path.join(output_data_folder_path, output_file_name)
        if not os.path.exists(target):
            write_tab_file(pyspark_df, output_data_folder_path, output_file_name)
            return
        rows_name = f"append_{os.getpid()}_{output_file_name}"
        write_tab_file(pyspark_df, output_data_folder_path, rows_name)
        rows_path = os.path.join(output_data_folder_path, rows_name)
        with open(rows_path, "rb") as src, open(target, "ab") as dst:
            src.readline()
            shutil.copyfileobj(src, dst)
        os.remove(rows_path)

    @classmethod
    def copy_data(
        cls,
        input_file_name: str,
        output_file_name: str,
        data_folder_path: str,
        output_data_folder_path: str,
    ) -> None:
        """Legacy ``copy_data`` (``cp -p``; silently does nothing on a missing source)."""
        data_folder_path = resolve_legacy_path(data_folder_path)
        output_data_folder_path = resolve_legacy_path(output_data_folder_path)
        os.makedirs(output_data_folder_path, exist_ok=True)
        src = os.path.join(data_folder_path, input_file_name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(output_data_folder_path, output_file_name))

    @classmethod
    def delete_directory(cls, directory: str) -> None:
        """Legacy ``delete_directory``."""
        shutil.rmtree(resolve_legacy_path(directory), ignore_errors=True)

    @classmethod
    def awk_merge(
        cls, input_directory: str, output_directory: str, prefix: str, output_name: str
    ) -> None:
        """Legacy ``awk_merge`` (``FNR==1 && NR!=1 {next} 1`` over ``prefix*.csv``).

        Shell semantics are kept: the ``> out`` redirection creates (truncates)
        the output first, and when the glob matches nothing ``awk`` exits 2,
        which legacy's ``subprocess.run(check=True)`` raised — so the output
        is left empty and the caller's remaining merges are skipped.
        """
        import subprocess

        input_directory = resolve_legacy_path(input_directory)
        output_directory = resolve_legacy_path(output_directory)
        paths = sorted(glob.glob(os.path.join(input_directory, f"{prefix}*.csv")))
        with open(output_directory + output_name, "wb") as out:
            if not paths:
                raise subprocess.CalledProcessError(
                    2, f"awk 'FNR==1 && NR!=1{{next}} 1' {input_directory}/{prefix}*.csv"
                )
            for i, path in enumerate(paths):
                with open(path, "rb") as src:
                    first = src.readline()
                    if i == 0:
                        out.write(first)
                    shutil.copyfileobj(src, out)

    # -- fixes -------------------------------------------------------------

    def apply_fix(
        self,
        fix_type: str,
        fix_name: str,
        version: str,
        input_path: str,
        output_path: str,
        fixed_table_name: str,
        mapped_table_name: str,
        input_data_folder: str,
        partner_name: str,
        src1: str,
        src2: str,
    ) -> None:
        """Legacy ``apply_fix`` — run the fix script in-process."""
        from ovid2.legacy.fix_stage import LegacyFixEntry, run_fix_script

        saved = list(_STATE["failures"])
        try:
            run_fix_script(
                LegacyFixEntry(fix_type, fix_name, version, src1, src2),
                partner=partner_name,
                folder_label=input_data_folder,
                mapped_table_name=mapped_table_name,
                fixed_table_name=fixed_table_name,
                input_path=input_path,
                output_path=output_path,
            )
        finally:
            _STATE["failures"] = saved + list(_STATE["failures"])

    @classmethod
    def _quarantine_failed_fixed_table(
        cls, output_path: str, fixed_table_name: str, fix_name: str, version: str
    ) -> None:
        """Legacy quarantine."""
        del fix_name, version
        quarantine(output_path, fixed_table_name)

    def __getattr__(self, name: str) -> Any:
        """Delegate geocoding/reference helpers to :mod:`ovid2.legacy.reference`."""
        from ovid2.config.settings import COMMON_LISTS_DIR
        from ovid2.legacy import reference

        fn = getattr(reference, name, None)
        if fn is None or name.startswith("_"):
            raise AttributeError(name)
        lists_dir = str(COMMON_LISTS_DIR).rstrip("/") + "/"
        return lambda *a, **k: fn(lists_dir, *a, **k)

    def read_from_snowflake(self, *args: Any, **kwargs: Any) -> DataFrame:
        """Not available in parity runs (see module docstring)."""
        del args, kwargs
        raise RuntimeError("read_from_snowflake is not available in the legacy_ovid fix stage")

    # -- terminal / logging (legacy wrote to a display queue) ------------

    @classmethod
    def print_failure_message(cls, folder: str, partner: str, job: str, text: str) -> None:
        """Record a failure (legacy ``_record_fix_failure``)."""
        _STATE["failures"].append(str(text))
        level = logging.WARNING if "does not exist" in str(text) else logging.ERROR
        logger.log(
            level, "legacy fix failure partner=%s folder=%s job=%s: %s", partner, folder, job, text
        )

    @classmethod
    def print_with_style(cls, text: Any, *args: Any, **kwargs: Any) -> None:
        """Legacy terminal print → log."""
        del args, kwargs
        logger.info("%s", text)

    @classmethod
    def print_fixer_status(cls, *args: Any, **kwargs: Any) -> None:
        """Legacy status line → debug log."""
        logger.debug("fixer status %s %s", args, kwargs)

    @classmethod
    def print_run_status(cls, *args: Any, **kwargs: Any) -> None:
        """Legacy status line → debug log."""
        logger.debug("run status %s %s", args, kwargs)


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


def install(spark: SparkSession, *, partner: str, folder_label: str, work_root: str = "") -> None:
    """Register the shim modules under their legacy names."""
    _STATE["spark"] = spark
    _STATE["work_root"] = work_root
    _STATE["partner"] = partner
    _STATE["folder"] = folder_label

    cf_mod = types.ModuleType("commonFunctions")
    cf_mod.CommonFuncitons = CommonFuncitons  # type: ignore[attr-defined]
    settings_mod = types.ModuleType("settings")
    for key, value in LEGACY_SETTINGS.items():
        setattr(settings_mod, key, value)
    log_mod = types.ModuleType("ovid_logging")
    log_mod.get_logger = lambda: logger  # type: ignore[attr-defined]
    log_mod.log_error = lambda message, exc_info=False: logger.error(message, exc_info=exc_info)  # type: ignore[attr-defined]
    log_mod.log_info = lambda message: logger.info(message)  # type: ignore[attr-defined]
    log_mod.log_warning = lambda message: logger.warning(message)  # type: ignore[attr-defined]
    sys.modules["commonFunctions"] = cf_mod
    sys.modules["settings"] = settings_mod
    sys.modules["ovid_logging"] = log_mod
    _install_path_hooks()


def uninstall() -> None:
    """Remove the path hooks (shim modules are restored by the caller)."""
    _remove_path_hooks()
    _STATE["spark"] = None


_ORIG: dict[str, Any] = {}


def _install_path_hooks() -> None:
    """Make ``spark.read.load`` / ``open`` / ``os.path.*`` honour legacy paths.

    A few vendored fixes build ``/app/common/...`` or ``common/lists/...``
    paths and hand them to ``spark.read.load`` / ``os.path.exists``. The
    hooks rewrite only those legacy prefixes and are idempotent.
    """
    from pyspark.sql.readwriter import DataFrameReader

    if "load" not in _ORIG:
        _ORIG["load"] = DataFrameReader.load
        _ORIG["csv"] = DataFrameReader.csv
        _ORIG["exists"] = _REAL_EXISTS
        _ORIG["isfile"] = _REAL_ISFILE

        def _load(self: Any, path: Any = None, *a: Any, **k: Any) -> Any:
            if isinstance(path, str):
                path = resolve_legacy_path(path)
            return _ORIG["load"](self, path, *a, **k)

        def _csv(self: Any, path: Any, *a: Any, **k: Any) -> Any:
            if isinstance(path, str):
                path = resolve_legacy_path(path)
            return _ORIG["csv"](self, path, *a, **k)

        def _exists(path: Any) -> bool:
            if isinstance(path, str):
                path = resolve_legacy_path(path)
            return bool(_ORIG["exists"](path))

        def _isfile(path: Any) -> bool:
            if isinstance(path, str):
                path = resolve_legacy_path(path)
            return bool(_ORIG["isfile"](path))

        DataFrameReader.load = _load  # type: ignore[method-assign]
        DataFrameReader.csv = _csv  # type: ignore[method-assign]
        os.path.exists = _exists
        os.path.isfile = _isfile


def _remove_path_hooks() -> None:
    """Restore the unpatched reader / filesystem probes."""
    if "load" not in _ORIG:
        return
    from pyspark.sql.readwriter import DataFrameReader

    DataFrameReader.load = _ORIG.pop("load")  # type: ignore[method-assign]
    DataFrameReader.csv = _ORIG.pop("csv")  # type: ignore[method-assign]
    os.path.exists = _ORIG.pop("exists")
    os.path.isfile = _ORIG.pop("isfile")
