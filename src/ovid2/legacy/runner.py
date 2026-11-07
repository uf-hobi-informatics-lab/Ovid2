"""Two-phase run orchestration for ``legacy_ovid`` pipelines.

Phase 1 — per table, through the normal Ovid2 transformer chain
(``format`` → ``deduplicate`` → ``map``; the legacy profile hooks inside
those transformers emulate legacy's stage boundaries), materialised as
``<scratch>/mapper_output/mapped_<t>.csv`` with the legacy single-file
writer.

Phase 2 — the verbatim legacy fix stage (:mod:`ovid2.legacy.fix_stage`)
over the scratch directory → ``fixer_output/fixed_<t>.csv``.

Phase 3 — each ``fixed_<t>.csv`` is read back with the legacy reader and
written to the output address. Because the legacy fixed file is already
the final artefact, a disc output receives exactly those bytes (the disc
single-file writer is the same writer); for other drivers the frame is
written through the driver as usual.

When ``fix`` is not among the requested transformations, phase 1's last
stage output is written directly (``formatted_`` / ``deduplicated_`` /
``mapped_`` view of the table), which is what the parity harness uses for
per-stage comparisons.

the adr-legacy-parity-port design note explains why the
fix stage cannot stay lazy for byte parity.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession

    from ovid2.core.addresses.base import BaseAddress
    from ovid2.core.engines.base import BaseEngine
    from ovid2.core.events import EventBus
    from ovid2.transformations.base import BaseTransformer

logger = logging.getLogger(__name__)

#: Stage name → legacy file prefix.
STAGE_PREFIX: dict[str, str] = {
    "format": "formatted_",
    "deduplicate": "deduplicated_",
    "map": "mapped_",
    "fix": "fixed_",
}


@dataclass
class LegacyRunResult:
    """Outcome of a legacy-profile run."""

    succeeded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


def scratch_root(pipeline: str, batch_id: str, instance_id: str) -> str:
    """Return (and create) the per-run legacy scratch directory."""
    from ovid2.config.settings import WORK_DIR

    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in batch_id) or "batch"
    root = os.path.join(str(WORK_DIR), "legacy_scratch", pipeline, safe, instance_id or "run")
    for sub in ("mapper_output", "fixer_output"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    return root


def _read_tab(spark: SparkSession, path: str) -> DataFrame:
    """Legacy fix-chain reader (``read_tab_csv``)."""
    return spark.read.load(
        path, format="csv", sep="\t", inferSchema="false", header="true", quote='"'
    )


def _disc_csv_root(address: BaseAddress) -> str | None:
    """Return the output directory when ``address`` is a single-file tab-CSV disc address."""
    root = getattr(address, "root_path", None)
    fmt = str(getattr(address, "format", "csv") or "csv")
    if root is None:
        view = getattr(address, "_merged_location_view", None)
        if callable(view):
            merged = view()
            root, fmt = merged.get("root"), str(merged.get("format") or "csv")
            if not merged.get("single_file", True):
                return None
    return str(root) if root and fmt == "csv" else None


def _publish_fixed(
    output_address: BaseAddress,
    spark: SparkSession,
    pipeline: str,
    batch_id: str,
    table: str,
    path: str,
) -> None:
    """Deliver ``fixed_<t>.csv`` to the output address.

    The legacy fixed file *is* the artefact legacy uploaded. For a disc
    CSV output it is copied byte-for-byte (a read → write round-trip would
    turn the writer's ``""`` empty-string token into an empty field). Any
    other driver receives the frame through the normal write path — the
    same text the legacy uploader loaded.
    """
    root = _disc_csv_root(output_address)
    if root is not None:
        os.makedirs(root, exist_ok=True)
        tmp = os.path.join(root, f".{table}.csv.partial")
        shutil.copyfile(path, tmp)
        os.replace(tmp, os.path.join(root, f"{table}.csv"))
        return
    output_address.write(_read_tab(spark, path), spark, pipeline, batch_id, table)


def _publish_stage(
    output_address: BaseAddress,
    spark: SparkSession,
    pipeline: str,
    batch_id: str,
    table: str,
    df: DataFrame,
    *,
    is_text: bool,
) -> None:
    """Deliver a pre-fix stage frame (``format`` / ``deduplicate`` / ``map`` view).

    A frame that is already legacy file text (the dedup stage output) goes
    to a disc CSV output verbatim — re-writing it through the CSV writer
    would escape it a second time. Other drivers get the reader view.
    """
    if not is_text:
        output_address.write(df, spark, pipeline, batch_id, table)
        return
    from ovid2.legacy.csv_roundtrip import legacy_read_normalise, write_legacy_text

    root = _disc_csv_root(output_address)
    if root is not None:
        write_legacy_text(df, root, f"{table}.csv")
        return
    output_address.write(legacy_read_normalise(df), spark, pipeline, batch_id, table)


def run_legacy_pipeline(
    *,
    engine: BaseEngine,
    spark: SparkSession,
    pipeline: str,
    batch_id: str,
    tables: list[str],
    transformations: list[BaseTransformer],
    transformation_names: list[str],
    input_address: BaseAddress,
    output_address: BaseAddress,
    input_address_name: str,
    bus: EventBus,
    instance_id: str,
    load_formatter: Any,
) -> LegacyRunResult:
    """Run a ``legacy_ovid`` pipeline end to end. See module docstring."""
    from ovid2.cdm.registry import resolve_cdm_for_pipeline
    from ovid2.core.events import table_completed, table_failed, table_started
    from ovid2.legacy.csv_roundtrip import LEGACY_TEXT_KEY, write_legacy_text
    from ovid2.legacy.fix_shim import write_tab_file
    from ovid2.legacy.fix_stage import fixer_tables, run_fix_stage
    from ovid2.transformations.fix import FixTransformer
    from ovid2.transformations.format import FormatTransformer

    if not getattr(engine, "session_is_spark", True):
        raise ValueError(f"legacy_ovid pipeline '{pipeline}' requires a Spark-family engine.")

    cdm = resolve_cdm_for_pipeline(pipeline)
    result = LegacyRunResult()
    want_fix = "fix" in transformation_names
    pre_fix = [t for t in transformations if not isinstance(t, (FormatTransformer, FixTransformer))]
    root = scratch_root(pipeline, batch_id, instance_id)
    mapper_out = os.path.join(root, "mapper_output")
    folder_label = batch_id

    # ---------------------------------------------------------------- phase 1
    produced: list[str] = []
    for table in tables:
        bus.emit(
            table_started(
                instance_id=instance_id, pipeline=pipeline, batch_id=batch_id, table=table
            )
        )
        try:
            formatter = load_formatter(
                pipeline,
                table,
                spark=spark,
                input_address=input_address,
                batch_id=batch_id,
                engine=engine,
                input_address_name=input_address_name,
            )
            df = formatter.format(None)
            context: dict[str, Any] = {
                "table_name": table,
                "pipeline": pipeline,
                "input_address": input_address,
                "spark": spark,
                "engine": engine,
                "batch_id": batch_id,
                "event_bus": bus,
                "instance_id": instance_id,
                "cdm": cdm,
            }
            for transform in pre_fix:
                df = transform.transform(df, context)
            is_text = bool(context.pop(LEGACY_TEXT_KEY, False))
            if want_fix:
                if is_text:
                    write_legacy_text(df, mapper_out, f"mapped_{table}.csv")
                else:
                    write_tab_file(df, mapper_out, f"mapped_{table}.csv")
            else:
                _publish_stage(
                    output_address, spark, pipeline, batch_id, table, df, is_text=is_text
                )
                bus.emit(
                    table_completed(
                        instance_id=instance_id, pipeline=pipeline, batch_id=batch_id, table=table
                    )
                )
                result.succeeded.append(table)
            produced.append(table)
        except Exception as exc:
            logger.exception("legacy table '%s' failed before fix", table)
            result.failed[table] = str(exc)
            bus.emit(
                table_failed(
                    instance_id=instance_id,
                    pipeline=pipeline,
                    batch_id=batch_id,
                    table=table,
                    error=str(exc),
                )
            )
    if not want_fix:
        return result

    # ---------------------------------------------------------------- phase 2
    # Legacy ran every ``<t>_fixer.py``; a table without one was never
    # fixed and so never uploaded (no ``fixed_<t>.csv``).
    with_fixer = set(fixer_tables(pipeline))
    fix_failures = run_fix_stage(
        spark,
        partner=pipeline,
        tables=[t for t in produced if t in with_fixer],
        work_root=root,
        folder_label=folder_label,
    )

    # ---------------------------------------------------------------- phase 3
    # Legacy uploaded *every* ``fixed_*.csv`` in ``fixer_output``
    # (``etl.get_partner_uploads_list``) — including tables a multi-table
    # fix created that had no formatter (eta ``loinc_mover_fix`` →
    # ``fixed_obs_clin.csv`` / ``fixed_obs_gen.csv``).
    fixer_out = os.path.join(root, "fixer_output")
    created = sorted(
        name[len("fixed_") : -len(".csv")]
        for name in os.listdir(fixer_out)
        if name.startswith("fixed_") and name.endswith(".csv")
    )
    publish = list(produced) + [t for t in created if t not in produced]
    for table in publish:
        path = os.path.join(fixer_out, f"fixed_{table}.csv")
        if table in fix_failures or not os.path.exists(path):
            error = fix_failures.get(table, f"legacy fix stage produced no fixed_{table}.csv")
            result.failed[table] = error
            bus.emit(
                table_failed(
                    instance_id=instance_id,
                    pipeline=pipeline,
                    batch_id=batch_id,
                    table=table,
                    error=error,
                )
            )
            continue
        try:
            _publish_fixed(output_address, spark, pipeline, batch_id, table, path)
            result.succeeded.append(table)
            bus.emit(
                table_completed(
                    instance_id=instance_id, pipeline=pipeline, batch_id=batch_id, table=table
                )
            )
        except Exception as exc:
            logger.exception("writing fixed_%s failed", table)
            result.failed[table] = str(exc)
            bus.emit(
                table_failed(
                    instance_id=instance_id,
                    pipeline=pipeline,
                    batch_id=batch_id,
                    table=table,
                    error=str(exc),
                )
            )
    if "multi_tables" in fix_failures:
        result.failed["multi_tables"] = fix_failures["multi_tables"]

    if os.environ.get("OVID2_LEGACY_KEEP_SCRATCH", "").strip() not in ("1", "true", "yes"):
        shutil.rmtree(root, ignore_errors=True)
    return result
