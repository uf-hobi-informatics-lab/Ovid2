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
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ovid2.core.cancellation import raise_if_cancelled

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
    """Return (and create) the per-run legacy scratch directory (real disk)."""
    from ovid2.config.settings import LEGACY_SCRATCH_ROOT

    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in batch_id) or "batch"
    root = os.path.join(
        str(LEGACY_SCRATCH_ROOT), "legacy_scratch", pipeline, safe, instance_id or "run"
    )
    for sub in ("mapper_output", "fixer_output"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    return root


def _read_tab(spark: SparkSession, path: str) -> DataFrame:
    """Legacy fix-chain reader (``read_tab_csv``) — every engine's ``read``."""
    return spark.read.load(
        path, format="csv", sep="\t", inferSchema="false", header="true", quote='"'
    )


@dataclass
class _Publisher:
    """Writes a table to the run's output on any engine.

    Spark engines keep the legacy ``BaseAddress.write`` path; every other
    engine persists through its stager (the same path ``run_pipeline``
    uses), so a legacy port can write to any storage type.
    """

    engine: Any
    output_address: Any
    output_address_name: str
    pipeline: str
    batch_id: str
    sf_overrides: dict[str, Any] | None = None
    cdm_name: str | None = None

    def write(self, df: Any, spark: Any, table: str) -> None:
        if getattr(self.engine, "session_is_spark", True):
            self.output_address.write(df, spark, self.pipeline, self.batch_id, table)
            return
        from ovid2.drivers.registry import driver_from_address

        driver = driver_from_address(
            self.output_address_name,
            pipeline=self.pipeline,
            sf_overrides=self.sf_overrides,
            cdm_name=self.cdm_name,
        )
        self.engine.get_stager().persist(df, driver, self.pipeline, self.batch_id, table)


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
    publisher: _Publisher,
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
    if not _has_header(path):
        # A fix that failed part-way (e.g. ``loinc_mover_fix`` after its
        # input was missing) can leave an empty ``fixed_<t>.csv``. Reading
        # it yields a zero-column frame and warehouses reject the DDL
        # (Snowflake: ``CREATE TABLE … ()`` → "unexpected ')'").
        raise ValueError(
            f"fixed_{table}.csv is empty (no header row); an earlier fix most likely "
            "failed — see the fix stage errors above"
        )
    publisher.write(_read_tab(spark, path), spark, table)


def _has_header(path: str) -> bool:
    """Return whether the tab file at ``path`` has a non-empty first line."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return bool(handle.readline().strip())
    except IsADirectoryError:
        return True  # a Spark part-file folder; let the reader decide


def _publish_stage(
    output_address: BaseAddress,
    spark: SparkSession,
    publisher: _Publisher,
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
        publisher.write(df, spark, table)
        return
    from ovid2.legacy.csv_roundtrip import legacy_read_normalise, write_legacy_text

    root = _disc_csv_root(output_address)
    if root is not None:
        write_legacy_text(df, root, f"{table}.csv")
        return
    publisher.write(legacy_read_normalise(df), spark, table)


def run_phase1(
    engine: Any,
    tables: list[str],
    one: Callable[[str], None],
    *,
    preload: Callable[[str], None] | None = None,
) -> None:
    """Run ``one(table)`` for every table, one at a time, in the given order.

    Every engine runs legacy tables serially (as every other pipeline):
    the running table gets the engine's whole capacity. Per-table failures
    are handled inside ``one``; a cancel / ``KeyboardInterrupt``
    propagates. ``engine`` and ``preload`` are accepted for call-site
    compatibility; formatter imports happen lazily inside ``one``.
    """
    del engine, preload
    for table in tables:
        one(table)


#: Stage label per pre-fix transformer class (``stage.progress`` ``stage``).
_STAGE_LABELS: dict[str, str] = {
    "DeduplicateTransformer": "deduplicate",
    "TransformStageTransformer": "transform",
    "MapTransformer": "map",
}


def _stage_label(transformer: Any) -> str:
    """Return the stage name a phase-1 transformer is reported under."""
    name = type(transformer).__name__
    label = _STAGE_LABELS.get(name)
    if label is not None:
        return label
    base = name[: -len("Transformer")] if name.endswith("Transformer") else name
    return "".join("_" + c.lower() if c.isupper() else c for c in base).lstrip("_")


def _fix_event(progress: Any, *, instance_id: str, pipeline: str, batch_id: str) -> Any:
    """Build the ``fix.applied`` / ``fix.skipped`` / ``fix.failed`` event of one legacy fix."""
    from ovid2.core.events import fix_applied, fix_failed, fix_skipped

    entry = progress.entry
    common: dict[str, Any] = {
        "instance_id": instance_id,
        "pipeline": pipeline,
        "batch_id": batch_id,
        "table": progress.table,
        "fix": entry.fix_name,
        "fix_type": entry.fix_type,
        "current_fix": progress.index,
        "total_fixes": progress.total,
        "src1": entry.src1,
        "src2": entry.src2,
    }
    if progress.skipped:
        return fix_skipped(reason=progress.error or "", **common)
    if progress.error is not None:
        return fix_failed(error=progress.error, **common)
    return fix_applied(**common)


def _scratch_entries() -> set[str]:
    """Names in this run's legacy round-trip folder (``legacy_rt-<instance>``)."""
    from ovid2.config.settings import LEGACY_SCRATCH_ROOT
    from ovid2.legacy.csv_roundtrip import run_scratch_dir

    root = run_scratch_dir()
    if os.path.abspath(root) == os.path.abspath(str(LEGACY_SCRATCH_ROOT)):
        return set()  # no per-run folder: never sweep the shared root
    try:
        return set(os.listdir(root))
    except OSError:
        return set()


def _drop_new_scratch(before: set[str]) -> None:
    """Remove round-trip dirs created since ``before`` (one table's scratch).

    Phase 1 runs one table at a time and every stage boundary of that table
    is executed by its ``mapped_<t>.csv`` write, so the ``legacy_rt_*`` /
    ``legacy_infer_*`` dirs it created are dead afterwards. Keeping them
    until the run ended filled the scratch disk on full-size feeds
    (acme 20260911: ~30 GB per large table → ENOSPC on ``vital``).
    """
    if _keep_scratch():
        return
    from ovid2.config.settings import LEGACY_SCRATCH_ROOT
    from ovid2.legacy.csv_roundtrip import run_scratch_dir

    root = run_scratch_dir()
    if os.path.abspath(root) == os.path.abspath(str(LEGACY_SCRATCH_ROOT)):
        return
    try:
        current = set(os.listdir(root))
    except OSError:
        return
    for name in current - before:
        if name.startswith(("legacy_rt_", "legacy_infer_")):
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)


def _keep_scratch() -> bool:
    return os.environ.get("OVID2_LEGACY_KEEP_SCRATCH", "").strip() in ("1", "true", "yes")


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
    output_address_name: str = "",
    sf_overrides: dict[str, Any] | None = None,
    cdm_name: str | None = None,
) -> LegacyRunResult:
    """Run a ``legacy_ovid`` pipeline end to end on any engine. See module docstring.

    The run's scratch (``legacy_scratch/…/<instance>`` and the round-trip
    folder ``legacy_rt-<instance>``) lives under ``LEGACY_SCRATCH_ROOT`` — real
    disk, tens of GB on a full feed — so it
    is removed however the run ends (success, table failure, error or
    cancel) unless ``OVID2_LEGACY_KEEP_SCRATCH`` is set; a crashed run's
    folders are reaped by :func:`ovid2.core.isolation.cleanup_stale_instances`.
    """
    from ovid2.legacy.csv_roundtrip import run_scratch_dir

    try:
        return _run_legacy_pipeline(
            engine=engine,
            spark=spark,
            pipeline=pipeline,
            batch_id=batch_id,
            tables=tables,
            transformations=transformations,
            transformation_names=transformation_names,
            input_address=input_address,
            output_address=output_address,
            input_address_name=input_address_name,
            bus=bus,
            instance_id=instance_id,
            load_formatter=load_formatter,
            output_address_name=output_address_name,
            sf_overrides=sf_overrides,
            cdm_name=cdm_name,
        )
    finally:
        if not _keep_scratch():
            root = scratch_root(pipeline, batch_id, instance_id)
            shutil.rmtree(root, ignore_errors=True)
            from ovid2.config.settings import LEGACY_SCRATCH_ROOT

            rt = run_scratch_dir()
            if os.path.abspath(rt) != os.path.abspath(str(LEGACY_SCRATCH_ROOT)):
                shutil.rmtree(rt, ignore_errors=True)


def _run_legacy_pipeline(
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
    output_address_name: str = "",
    sf_overrides: dict[str, Any] | None = None,
    cdm_name: str | None = None,
) -> LegacyRunResult:
    """Body of :func:`run_legacy_pipeline` (which owns scratch cleanup)."""
    from ovid2.cdm.registry import resolve_cdm_for_pipeline
    from ovid2.core.events import (
        TABLE_STAGE_ACTIVE,
        TABLE_STAGE_WAITING,
        table_completed,
        table_failed,
        table_stage,
        table_started,
    )
    from ovid2.legacy.csv_roundtrip import LEGACY_TEXT_KEY, write_legacy_text
    from ovid2.legacy.fix_shim import write_tab_file
    from ovid2.legacy.fix_stage import FixProgress, fixer_tables, run_fix_stage
    from ovid2.transformations.fix import FixTransformer
    from ovid2.transformations.format import FormatTransformer

    cdm = resolve_cdm_for_pipeline(pipeline)
    publisher = _Publisher(
        engine=engine,
        output_address=output_address,
        output_address_name=output_address_name,
        pipeline=pipeline,
        batch_id=batch_id,
        sf_overrides=sf_overrides,
        cdm_name=cdm_name,
    )
    result = LegacyRunResult()
    want_fix = "fix" in transformation_names
    pre_fix = [t for t in transformations if not isinstance(t, (FormatTransformer, FixTransformer))]
    pre_fix_stages = [_stage_label(t) for t in pre_fix]
    root = scratch_root(pipeline, batch_id, instance_id)
    mapper_out = os.path.join(root, "mapper_output")
    folder_label = batch_id

    def _step(table: str, stage: str) -> None:
        # Stage marker inside phase 1: the table is (still) active; the
        # event only names the step it is in (format / deduplicate / map …).
        _stage(table, stage, TABLE_STAGE_ACTIVE)

    def _on_fix(progress: FixProgress) -> None:
        bus.emit(
            _fix_event(progress, instance_id=instance_id, pipeline=pipeline, batch_id=batch_id)
        )

    def _stage(table: str, stage: str, state: str) -> None:
        # Phase boundary: the per-table clocks only count the time a table
        # is actually being worked on (phase 1 runs every table before
        # phase 2 fixes any, so a mapped table *waits* in between).
        bus.emit(
            table_stage(
                instance_id=instance_id,
                pipeline=pipeline,
                batch_id=batch_id,
                table=table,
                stage=stage,
                state=state,
            )
        )

    # ---------------------------------------------------------------- phase 1
    produced_set: set[str] = set()
    lock = threading.Lock()

    from ovid2.core.pause import gate as _pause_gate

    hold = _pause_gate(bus, instance_id=instance_id, pipeline=pipeline, batch_id=batch_id)

    def _phase1_one(table: str) -> None:
        raise_if_cancelled()
        hold()
        bus.emit(
            table_started(
                instance_id=instance_id, pipeline=pipeline, batch_id=batch_id, table=table
            )
        )
        scratch_before = _scratch_entries()
        try:
            _phase1_body(table)
        finally:
            # The table's round-trip files are consumed once its output is
            # written (or it failed); free the tmpfs now instead of at run end.
            _drop_new_scratch(scratch_before)

    def _phase1_body(table: str) -> None:
        try:
            _step(table, "format")
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
            for transform, label in zip(pre_fix, pre_fix_stages, strict=True):
                _step(table, label)
                df = transform.transform(df, context)
            is_text = bool(context.pop(LEGACY_TEXT_KEY, False))
            # Every stage above is lazy; the write below executes them all.
            _step(table, "execute")
            if want_fix:
                if is_text:
                    write_legacy_text(df, mapper_out, f"mapped_{table}.csv")
                else:
                    write_tab_file(df, mapper_out, f"mapped_{table}.csv")
                _stage(table, "map", TABLE_STAGE_WAITING)
            else:
                _publish_stage(output_address, spark, publisher, table, df, is_text=is_text)
                bus.emit(
                    table_completed(
                        instance_id=instance_id, pipeline=pipeline, batch_id=batch_id, table=table
                    )
                )
                with lock:
                    result.succeeded.append(table)
            with lock:
                produced_set.add(table)
        except Exception as exc:
            # A cancel can surface as an ordinary engine error (DuckDB:
            # "Query interrupted"); stop instead of starting the next table.
            raise_if_cancelled(exc)
            logger.exception("legacy table '%s' failed before fix", table)
            with lock:
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

    started = time.monotonic()
    run_phase1(engine, tables, _phase1_one)
    logger.info("Legacy phase 1 (format/dedup/map) finished in %.1fs", time.monotonic() - started)
    # Keep the requested table order downstream (fix stage, publishing).
    produced: list[str] = [t for t in tables if t in produced_set]
    if not want_fix:
        return result

    # ---------------------------------------------------------------- phase 2
    # Legacy ran every ``<t>_fixer.py``; a table without one was never
    # fixed and so never uploaded (no ``fixed_<t>.csv``).
    with_fixer = set(fixer_tables(pipeline))
    started = time.monotonic()
    fix_failures = run_fix_stage(
        spark,
        partner=pipeline,
        tables=[t for t in produced if t in with_fixer],
        work_root=root,
        folder_label=folder_label,
        on_table=lambda table, state: _stage(table, "fix", state),
        on_fix=_on_fix,
    )
    logger.info("Legacy phase 2 (fix) finished in %.1fs", time.monotonic() - started)

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
        raise_if_cancelled()
        hold()
        _stage(table, "write", TABLE_STAGE_ACTIVE)
        try:
            _publish_fixed(output_address, spark, publisher, table, path)
            result.succeeded.append(table)
            bus.emit(
                table_completed(
                    instance_id=instance_id, pipeline=pipeline, batch_id=batch_id, table=table
                )
            )
        except Exception as exc:
            raise_if_cancelled(exc)
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

    return result
