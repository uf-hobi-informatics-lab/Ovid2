"""Runtime for legacy-Ovid formatters ported verbatim (``legacy_ovid`` profile).

Every legacy partner formatter is a script that

1. builds ``spark`` / ``cf = CommonFuncitons(partner)``;
2. reads raw files from ``/data/<folder>/<glob>`` with ``spark.read.load``
   (or ``cf.spark_read`` / ``cf.spark_read_omop_concept``);
3. transforms them (selects, joins, local Python UDFs, ``cf.*`` UDFs);
4. calls ``cf.write_pyspark_output_file(df, "formatted_<t>.csv", …)``.

:mod:`ovid2.tools.legacy_formatter_migrator` rewrites each script into a
``formatters_custom/<t>_formatter.py`` whose generated ``_legacy_body``
function holds steps 2–3 **unchanged** except for three mechanical
substitutions:

* the input folder becomes ``rt.input_dir`` (the Ovid2 input address);
* the shared material paths (``/app/common/omop_cdm``,
  ``/app/common/lists``) become ``rt.omop_dir`` / ``rt.lists_dir``;
* ``cf`` becomes a :class:`LegacyCommonFunctions` shim and the final
  ``cf.write_pyspark_output_file`` becomes ``rt.emit(df)`` — the frame is
  *returned* to the Ovid2 pipeline instead of written.

So the transformation logic, including every local UDF, is byte-for-byte
the legacy code. This module provides the runtime those bodies call.
"""

from __future__ import annotations

import glob
import importlib
import os
import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ovid2.formatters.base import BaseFormatter

if TYPE_CHECKING:
    from collections.abc import Callable

    from pyspark.sql import DataFrame, SparkSession


class LegacyFormatterError(RuntimeError):
    """Raised when a ported legacy body did not produce its output frame."""


@dataclass
class LegacyRuntime:
    """Per-call runtime handed to a generated ``_legacy_body``.

    Attributes:
        spark: Active Spark session.
        pipeline: Ovid2 pipeline name (legacy *partner*).
        input_dir: Raw input directory (trailing ``/``) — legacy
            ``/data/<folder>/``.
        omop_dir: OMOP vocabulary directory (trailing ``/``) — legacy
            ``/app/common/omop_cdm/``.
        lists_dir: Shared lists directory (trailing ``/``) — legacy
            ``/app/common/lists/``.
        work_dir: Scratch directory standing in for legacy
            ``formatter_output/`` (trailing ``/``). Side files a formatter
            writes (e.g. ``raw_formatted_hash_token.csv`` fed to Datavant)
            land here for real.
        datavant_dir: Scratch directory standing in for legacy
            ``data/datavant_output/<folder>/`` (trailing ``/``).
        table: Table this formatter produces.
        output_file: The legacy output file name for :attr:`table`.
        folder_label: Legacy ``input_data_folder`` value (messages only).
    """

    spark: SparkSession
    pipeline: str
    input_dir: str
    omop_dir: str
    lists_dir: str
    work_dir: str
    datavant_dir: str
    table: str
    output_file: str
    folder_label: str = ""
    outputs: dict[str, DataFrame] = field(default_factory=dict)

    def emit(self, df: DataFrame, output_file_name: str) -> None:
        """Record ``df`` as the legacy file ``output_file_name``.

        The table's own output (``formatted_<t>.csv``) is captured lazily —
        Ovid2 writes it at the end of the pipeline. Any *other* file is a
        side file a later step reads back, so it is written for real with
        the legacy single-file tab writer.
        """
        if output_file_name == self.output_file:
            self.outputs[output_file_name] = df
            return
        write_legacy_csv(df, self.work_dir, output_file_name)

    def module_name(self, legacy_module: str) -> str:
        """Map a legacy ``partners.<p>.<module>`` import to its Ovid2 module.

        Any other module name is returned unchanged.
        """
        if not legacy_module.startswith("partners."):
            return legacy_module
        leaf = legacy_module.rsplit(".", 1)[-1]
        leaf = {"partner_settings": "pipeline_settings"}.get(leaf, leaf)
        return f"ovid2_pipelines.{self.pipeline}.{leaf}"

    def partner_settings(self) -> Any:
        """Return the pipeline's ``pipeline_settings`` (legacy ``partner_settings``)."""
        return importlib.import_module(
            self.module_name(f"partners.{self.pipeline}.partner_settings")
        )

    def partner_dictionaries(self) -> Any:
        """Return the pipeline's ``dictionaries`` module."""
        return importlib.import_module(self.module_name(f"partners.{self.pipeline}.dictionaries"))

    def dictionaries_path(self) -> str:
        """Return the filesystem path of the pipeline's ``dictionaries.py``."""
        mod = importlib.import_module(self.module_name(f"partners.{self.pipeline}.dictionaries"))
        return str(mod.__file__)

    def read_output(self) -> DataFrame:
        """Return the table output: captured frame, else the side file read back."""
        if self.output_file in self.outputs:
            return self.outputs[self.output_file]
        path = os.path.join(self.work_dir, self.output_file)
        if os.path.exists(path):
            return self.spark.read.load(
                path, format="csv", sep="\t", inferSchema="false", header="true", quote='"'
            )
        raise LegacyFormatterError(
            f"legacy body did not produce {self.output_file!r} (captured: {sorted(self.outputs)})."
        )


def write_legacy_csv(df: DataFrame, folder: str, output_file_name: str) -> None:
    """Write ``df`` exactly like legacy ``cf.write_pyspark_output_file``.

    One tab-delimited file, header from the schema, quoting disabled,
    ``NullType`` columns cast to string — the Ovid2 disc single-file writer
    implements exactly that contract.
    """
    from ovid2.drivers.disc.spark import write_dataframe

    os.makedirs(folder, exist_ok=True)
    stem = (
        output_file_name[: -len(".csv")] if output_file_name.endswith(".csv") else output_file_name
    )
    write_dataframe(df, folder.rstrip("/"), stem, "\t", "csv", single_file=True)


class LegacyCommonFunctions:
    """The subset of legacy ``CommonFuncitons`` that formatters call.

    UDF attributes are the byte-exact ports from :mod:`ovid2.legacy.udfs`;
    readers use the legacy reader options; geocoding helpers read the
    shared lists directory.
    """

    def __init__(self, rt: LegacyRuntime) -> None:
        from ovid2.legacy.udfs import legacy_udfs

        self._rt = rt
        udfs = legacy_udfs(rt.pipeline)
        self.partner = rt.pipeline
        self.format_date_udf = udfs.format_date
        self.format_time_udf = udfs.format_time
        self.get_time_from_datetime_udf = udfs.get_time_from_datetime
        self.get_date_from_datetime_udf = udfs.get_date_from_datetime
        self.return_if_float_udf = udfs.return_if_float
        self.copy_if_float_udf = udfs.copy_if_float
        self.get_valid_zip5_udf = udfs.get_valid_zip5
        self.get_current_time_udf = udfs.current_time

    def factory(self, partner: str = "") -> LegacyCommonFunctions:
        """Stand-in for ``CommonFuncitons(partner)`` inside a ported body."""
        del partner
        return LegacyCommonFunctions(self._rt)

    # -- static helpers some formatters call directly ------------------- #

    @staticmethod
    def format_date(val: Any) -> str | None:
        """Legacy ``cf.format_date`` (plain Python)."""
        from ovid2.legacy.udfs import format_date

        return format_date(val)

    @staticmethod
    def format_time(val: Any) -> str:
        """Legacy ``cf.format_time`` (plain Python)."""
        from ovid2.legacy.udfs import format_time

        return format_time(val)

    # -- readers -------------------------------------------------------- #

    def spark_read(self, file_path: str, spark: SparkSession) -> DataFrame:
        """Legacy ``cf.spark_read`` (tab CSV, all strings)."""
        return spark.read.csv(
            file_path,
            sep="\t",
            header=True,
            quote='"',
            inferSchema=False,
            multiLine=False,
            escape='"',
        )

    def spark_read_omop_concept(self, file_path: str, spark: SparkSession) -> DataFrame:
        """Legacy ``cf.spark_read_omop_concept`` (columns lower-cased)."""
        df = self.spark_read(file_path, spark)
        return df.toDF(*[c.lower() for c in df.columns])

    # -- outputs / files ------------------------------------------------ #

    def write_pyspark_output_file(
        self, payspark_df: DataFrame, output_file_name: str, output_data_folder_path: str
    ) -> None:
        """Capture (own output) or write (side file) — see :meth:`LegacyRuntime.emit`."""
        del output_data_folder_path  # rewritten to rt.work_dir by the migrator
        self._rt.emit(payspark_df, output_file_name)

    def copy_data(
        self,
        input_file_name: str,
        output_file_name: str,
        data_folder_path: str,
        output_data_folder_path: str,
    ) -> None:
        """Legacy ``cf.copy_data`` — ``cp -p`` one file."""
        os.makedirs(output_data_folder_path, exist_ok=True)
        shutil.copy2(
            os.path.join(data_folder_path, input_file_name),
            os.path.join(output_data_folder_path, output_file_name),
        )

    def delete_directory(self, directory: str) -> None:
        """Legacy ``cf.delete_directory`` — ``rm -r``."""
        shutil.rmtree(directory, ignore_errors=True)

    def awk_merge(
        self, input_directory: str, output_directory: str, prefix: str, output_name: str
    ) -> None:
        """Legacy ``cf.awk_merge`` — concatenate ``prefix*.csv`` keeping one header."""
        paths = sorted(glob.glob(os.path.join(input_directory, f"{prefix}*.csv")))
        with open(os.path.join(output_directory, output_name), "wb") as out:
            for i, path in enumerate(paths):
                with open(path, "rb") as src:
                    first = src.readline()
                    if i == 0:
                        out.write(first)
                    shutil.copyfileobj(src, out)

    # -- Datavant ------------------------------------------------------- #

    def _datavant_key(self) -> str:
        settings = self._rt.partner_settings()
        return str(settings.DATAVANT_KEY)

    def datavant_transfer_tokens_to_pcornet_parallel(
        self,
        input_file_path: str,
        file_name: str,
        output_file_path: str = "",
        folder: str = "",
        partner: str = "",
    ) -> None:
        """Legacy parallel Datavant transform into ``rt.datavant_dir``."""
        del output_file_path, folder, partner
        from ovid2.legacy.datavant import transfer_tokens_to_pcornet_parallel

        transfer_tokens_to_pcornet_parallel(
            input_file_path, file_name, self._rt.datavant_dir, self._datavant_key()
        )

    def datavant_transfer_tokens_to_pcornet(
        self,
        input_file_path: str,
        file_name: str,
        output_file_path: str = "",
        folder: str = "",
        partner: str = "",
    ) -> None:
        """Legacy sequential Datavant transform into ``rt.datavant_dir``."""
        del output_file_path, folder, partner
        from ovid2.legacy.datavant import transfer_tokens_to_pcornet

        transfer_tokens_to_pcornet(
            input_file_path, file_name, self._rt.datavant_dir, self._datavant_key()
        )

    def datavant_export_to_datavant_inbox(self, *args: Any, **kwargs: Any) -> None:
        """Ancillary match-inbox export — out of parity scope; no-op."""
        del args, kwargs

    # -- no-op terminal / logging helpers ------------------------------- #

    def print_with_style(self, *args: Any, **kwargs: Any) -> None:
        """Legacy terminal helper — swallowed (events cover progress)."""
        del args, kwargs

    def print_failure_message(self, *args: Any, **kwargs: Any) -> None:
        """Legacy failure helper — never reached (bodies re-raise)."""
        del args, kwargs

    def get_spark_session(self, app_name: str) -> Any:
        """Return the pipeline's Spark session (``stop()`` is a no-op)."""
        del app_name
        from ovid2.legacy.fix_shim import NonStoppingSession

        return NonStoppingSession(self._rt.spark)

    # -- geocoding / reference lookups ---------------------------------- #

    def __getattr__(self, name: str) -> Any:
        """Delegate geocoding/reference helpers to :mod:`ovid2.legacy.reference`."""
        from ovid2.legacy import reference

        fn = getattr(reference, name, None)
        if fn is None:
            raise AttributeError(
                f"Legacy CommonFuncitons.{name} is not ported to ovid2.legacy "
                "(add it to ovid2.legacy.reference)."
            )
        if name in reference.COLUMN_HELPERS:
            return fn  # pure Column expression helper — no lists dir
        return lambda *a, **k: fn(self._rt.lists_dir, *a, **k)


class LegacyFormatter(BaseFormatter):
    """Base class for generated ``formatters_custom`` legacy ports.

    Subclasses set :attr:`TABLE`, :attr:`INPUT_GLOB`, :attr:`OUTPUT_FILE`
    and :attr:`BODY` (the generated ``_legacy_body(rt, cf, spark, …)``).
    """

    requires_family: str | None = "spark"

    #: CDM table name.
    TABLE: str = ""
    #: The primary input glob (used only for the placeholder read).
    INPUT_GLOB: str = "*"
    #: Legacy output file the body writes (``formatted_<t>.csv``).
    OUTPUT_FILE: str = ""
    #: Generated body.
    BODY: Callable[..., None] | None = None

    @property
    def table_name(self) -> str:
        """Return the CDM table name."""
        return self.TABLE

    @property
    def input_file_name(self) -> str:
        """Return the primary input glob."""
        return self.INPUT_GLOB

    @property
    def primary_source_optional(self) -> bool:
        """Legacy bodies read their own inputs; the placeholder may be absent."""
        return True

    def _runtime(self) -> LegacyRuntime:
        """Build the :class:`LegacyRuntime` from the adapter context."""
        from ovid2.config.settings import COMMON_LISTS_DIR, OMOP_CDM_DIR, WORK_DIR

        ctx = self.context
        root = getattr(ctx.input_address, "root_path", None)
        if root is None:
            merged = getattr(ctx.input_address, "_merged_location_view", None)
            root = merged()["root"] if callable(merged) else None
        if root is None:
            raise LegacyFormatterError(
                f"{type(self).__name__}: legacy formatters read raw files from a "
                "disc input address; the configured input address has no filesystem root."
            )
        scratch = os.path.join(str(WORK_DIR), "legacy_scratch", ctx.pipeline, _safe(ctx.batch_id))
        work = os.path.join(scratch, "formatter_output")
        datavant = os.path.join(scratch, "datavant_output")
        os.makedirs(work, exist_ok=True)
        return LegacyRuntime(
            spark=ctx.spark,
            pipeline=ctx.pipeline,
            input_dir=str(root).rstrip("/") + "/",
            omop_dir=str(OMOP_CDM_DIR).rstrip("/") + "/",
            lists_dir=str(COMMON_LISTS_DIR).rstrip("/") + "/",
            work_dir=work + "/",
            datavant_dir=datavant + "/",
            table=self.TABLE,
            output_file=self.OUTPUT_FILE,
            folder_label=os.path.basename(str(root).rstrip("/")),
        )

    def format(self, df: DataFrame) -> DataFrame:
        """Run the ported legacy body and return its output frame."""
        del df  # the body reads its own inputs, like legacy
        body = type(self).BODY
        if body is None:
            raise LegacyFormatterError(f"{type(self).__name__} has no BODY.")
        rt = self._runtime()
        from ovid2.legacy.udfs import register_sql_functions

        register_sql_functions(rt.spark, rt.pipeline)
        from ovid2.legacy.fix_shim import NonStoppingSession

        body(rt, LegacyCommonFunctions(rt), NonStoppingSession(rt.spark))
        return rt.read_output()


def _safe(label: str) -> str:
    """Return ``label`` as a single safe path component."""
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in label) or "batch"
