"""Fix transformer.

Loads per-pipeline, per-table fix configurations and applies them as a chain
of lazy Spark transformations. Fixes are declared in
``ovid2_pipelines.<pipeline>.fixer_configs.FIXER_CHAINS``.

Contract:

- Any fix registered in
  :data:`ovid2.transformations.fixers.common.registry.COMMON_FIXES` runs.
- Unknown fix names are skipped with a warning.
- If any fix raises, the pre-fix DataFrame is preserved and the pipeline
  continues; the error is emitted on the event bus (P3) — currently
  ``pipeline_logger.log_fix_error`` when a legacy logger is provided.
- Named-field aliases (``reference_table``, ``target_column``,
  ``list_name``, …) are normalised to the legacy ``src1``/``src2`` shape
  before ``BaseFixer.apply`` is called — see
  :func:`ovid2.transformations.fixers.common.registry.normalise_fix_config`.
- Cross-table fixes declared in
  ``ovid2_pipelines.<pipeline>.fixer_configs.MULTI_TABLE_FIXES`` are
  applied by :meth:`FixTransformer.apply_multi_table_fixes`, which the
  caller (typically the CLI or the scheduler) invokes once per pipeline
  after every per-table pipeline has finished.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any

from ovid2.core.events import fix_applied, fix_failed
from ovid2.transformations.base import BaseTransformer
from ovid2.transformations.fixers.common.registry import COMMON_FIXES, normalise_fix_config

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession

logger = logging.getLogger(__name__)


class FixTransformer(BaseTransformer):
    """Apply a pipeline's declarative fix chain to a table."""

    # Fixes that read a reference file from ``material/lists/<src1>.csv``.
    _FILE_BASED_FIXES: frozenset[str] = frozenset({"list_remover", "patterns_remover"})

    def transform(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Run every fix in ``FIXER_CHAINS[table_name]`` in order."""
        table_name = context.get("table_name")
        pipeline = context.get("pipeline")
        batch_id = context.get("batch_id") or ""
        instance_id = context.get("instance_id") or ""
        bus = context.get("event_bus")
        pipeline_logger = context.get("pipeline_logger")

        if table_name is None or pipeline is None:
            return df

        fix_chain = self._load_fix_chain(pipeline, table_name)
        if not fix_chain:
            return df

        total_fixes = len(fix_chain)

        for fix_index, raw_config in enumerate(fix_chain, start=1):
            fix_config = normalise_fix_config(raw_config)
            fix_name = str(fix_config.get("fix", ""))
            src1_value = fix_config.get("src1", "")
            src2_value = fix_config.get("src2", "")

            if not fix_name:
                logger.warning(
                    "Empty fix name in config for pipeline '%s' table '%s', skipping",
                    pipeline,
                    table_name,
                )
                continue

            is_common = fix_name in COMMON_FIXES
            fix_type = "common" if is_common else "custom"
            if not is_common:
                logger.warning(
                    "Unknown fix '%s' for pipeline '%s' table '%s', skipping",
                    fix_name,
                    pipeline,
                    table_name,
                )
                continue

            file_name = (
                f"{src1_value}.csv" if (fix_name in self._FILE_BASED_FIXES and src1_value) else ""
            )

            fix_context = {**context, **fix_config}
            fixer = COMMON_FIXES[fix_name]()

            try:
                df = fixer.apply(df, fix_context)
            except Exception as exc:
                if bus is not None:
                    bus.emit(
                        fix_failed(
                            instance_id=instance_id,
                            pipeline=str(pipeline),
                            batch_id=str(batch_id),
                            table=str(table_name),
                            fix=fix_name,
                            error=str(exc),
                            fix_type=fix_type,
                            current_fix=fix_index,
                            total_fixes=total_fixes,
                            src1=str(src1_value) if src1_value else "",
                            src2=str(src2_value) if src2_value else "",
                            file_name=file_name,
                        )
                    )
                if pipeline_logger is not None:
                    pipeline_logger.log_fix_error(
                        fix_name,
                        str(exc),
                        current_fix=fix_index,
                        total_fixes=total_fixes,
                        fix_type=fix_type,
                        src1=str(src1_value) if src1_value else "",
                        src2=str(src2_value) if src2_value else "",
                        file_name=file_name,
                    )
                elif bus is None:
                    logger.error(
                        "Fix '%s' failed for pipeline '%s' table '%s': %s",
                        fix_name,
                        pipeline,
                        table_name,
                        exc,
                    )
                continue

            if bus is not None:
                bus.emit(
                    fix_applied(
                        instance_id=instance_id,
                        pipeline=str(pipeline),
                        batch_id=str(batch_id),
                        table=str(table_name),
                        fix=fix_name,
                        fix_type=fix_type,
                        current_fix=fix_index,
                        total_fixes=total_fixes,
                        src1=str(src1_value) if src1_value else "",
                        src2=str(src2_value) if src2_value else "",
                        file_name=file_name,
                    )
                )
            if pipeline_logger is not None:
                pipeline_logger.log_fix_applied(
                    fix_name,
                    current_fix=fix_index,
                    total_fixes=total_fixes,
                    fix_type=fix_type,
                    src1=str(src1_value) if src1_value else "",
                    src2=str(src2_value) if src2_value else "",
                    file_name=file_name,
                )
            elif bus is None:
                logger.debug(
                    "Applied fix '%s' to '%s' for pipeline '%s'",
                    fix_name,
                    table_name,
                    pipeline,
                )
        return df

    @staticmethod
    def _load_fix_chain(pipeline: str, table_name: str) -> list[dict[str, Any]]:
        """Return ``FIXER_CHAINS[table_name]`` from the pipeline module, or ``[]``."""
        module_name = f"ovid2_pipelines.{pipeline}.fixer_configs"
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            return []
        fixer_chains = getattr(module, "FIXER_CHAINS", {})
        result: list[dict[str, Any]] = list(fixer_chains.get(table_name, []))
        return result

    @staticmethod
    def load_multi_table_fixes(pipeline: str) -> list[dict[str, Any]]:
        """Return ``MULTI_TABLE_FIXES`` from the pipeline's ``fixer_configs``."""
        module_name = f"ovid2_pipelines.{pipeline}.fixer_configs"
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            return []
        return list(getattr(module, "MULTI_TABLE_FIXES", []))

    @classmethod
    def apply_multi_table_fixes(
        cls,
        pipeline: str,
        *,
        spark: SparkSession,
        input_address: Any,
        batch_id: str,
        output_address: Any | None = None,
        event_bus: Any | None = None,
        instance_id: str = "",
    ) -> None:
        """Apply ``MULTI_TABLE_FIXES`` for ``pipeline``.

        Each entry is a fixer-config dict (same shape as a per-table
        entry) that operates across two or more registered temp views.
        Multi-table fixes are typically LOINC movers, cross-table code
        moves, or reference-integrity re-checks that only make sense
        after every per-table pipeline has completed.

        Called once per pipeline by the CLI (``ovid2 etl``) after all
        per-table pipelines finish. Each fix reads its inputs as Spark
        temp views (registered during the per-table pipelines) and
        writes its outputs via ``output_address.write`` when supplied.

        Args:
            pipeline: Pipeline name.
            spark: Active Spark session.
            input_address: Where reference tables live.
            batch_id: Batch identifier.
            output_address: When set, each fix writes its output DataFrame
                via ``output_address.write``. When ``None``, the fix runs
                for its side effects (temp-view replacement) only.
            event_bus: Optional event bus for observability.
            instance_id: Owning instance id (for event emission).
        """
        fixes = cls.load_multi_table_fixes(pipeline)
        if not fixes:
            return

        total = len(fixes)
        for index, raw_config in enumerate(fixes, start=1):
            fix_config = normalise_fix_config(raw_config)
            fix_name = str(fix_config.get("fix", ""))
            table_name = str(fix_config.get("table", "")) or fix_name

            if fix_name not in COMMON_FIXES:
                logger.warning(
                    "Unknown multi-table fix '%s' for pipeline '%s'; skipping",
                    fix_name,
                    pipeline,
                )
                continue

            fixer = COMMON_FIXES[fix_name]()
            context: dict[str, Any] = {
                "spark": spark,
                "input_address": input_address,
                "pipeline": pipeline,
                "batch_id": batch_id,
                "table_name": table_name,
                "event_bus": event_bus,
                "instance_id": instance_id,
                **fix_config,
            }

            # Multi-table fixes typically source their own base DataFrame
            # from a spark temp view named ``table``.
            try:
                base_df = spark.table(table_name)
            except Exception as exc:
                logger.warning(
                    "Multi-table fix '%s' skipped for pipeline '%s': base table '%s' unavailable: %s",
                    fix_name,
                    pipeline,
                    table_name,
                    exc,
                )
                continue

            try:
                result_df = fixer.apply(base_df, context)
            except Exception as exc:
                if event_bus is not None:
                    event_bus.emit(
                        fix_failed(
                            instance_id=instance_id,
                            pipeline=pipeline,
                            batch_id=batch_id,
                            table=table_name,
                            fix=fix_name,
                            error=str(exc),
                            fix_type="multi_table",
                            current_fix=index,
                            total_fixes=total,
                            src1=str(fix_config.get("src1", "")),
                            src2=str(fix_config.get("src2", "")),
                            file_name="",
                        )
                    )
                logger.error(
                    "Multi-table fix '%s' failed for pipeline '%s': %s",
                    fix_name,
                    pipeline,
                    exc,
                )
                continue

            result_df.createOrReplaceTempView(table_name)
            if output_address is not None:
                output_address.write(result_df, spark, pipeline, batch_id, table_name)

            if event_bus is not None:
                event_bus.emit(
                    fix_applied(
                        instance_id=instance_id,
                        pipeline=pipeline,
                        batch_id=batch_id,
                        table=table_name,
                        fix=fix_name,
                        fix_type="multi_table",
                        current_fix=index,
                        total_fixes=total,
                        src1=str(fix_config.get("src1", "")),
                        src2=str(fix_config.get("src2", "")),
                        file_name="",
                    )
                )
