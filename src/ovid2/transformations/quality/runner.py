"""Data-health check runner.

Executes the declarative checks selected from
:mod:`ovid2.transformations.quality.registry` against an input source and
produces:

1. A list of :class:`~ovid2.transformations.quality.results.CheckOutcome`
   objects (for the JSON report artifact the web viewer reads).
2. A lazy metric-row DataFrame in the canonical ``DCQ_RESULTS`` shape, ready
   to be written to the output storage account as a durable, queryable
   ``dcq_results`` table.

Unlike :func:`ovid2.core.pipeline.run_pipeline` (which is table-centric and
runs the format→dedup→map→fix chain), health checks are cross-table and read
the CDM tables *as they already are* at the input. So the runner is a
self-contained orchestrator invoked by the ``ovid2 health`` CLI.

Laziness note
-------------

Health checks are an **analysis pass**, like the existing ``mapping_gap`` /
``distinct_values`` transformers. Operators build lazy aggregations; the one
place a materialising ``.count()`` happens is :meth:`_count`, marked
``# noqa: OVID2001`` per the same contract-test allowance those transformers
use. No driver-side ``.collect()`` is ever issued — the durable results
table is written through the normal lazy write path.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from ovid2.algorithms.registry import registry as algorithms
from ovid2.core.events import (
    EventBus,
    check_completed,
    check_failed,
    check_started,
    get_default_bus,
    warning_event,
)
from ovid2.transformations.quality.ops import OpContext
from ovid2.transformations.quality.results import (
    CheckOutcome,
)

if TYPE_CHECKING:
    from ovid2.cdm.handle import CdmHandle
    from ovid2.core.addresses.base import BaseAddress
    from ovid2.core.engines.base import BaseEngine
    from ovid2.drivers.base import Driver
    from ovid2.transformations.quality.registry import CheckSpec

logger = logging.getLogger(__name__)


class HealthCheckRunner:
    """Run the selected data-health checks against one input source."""

    def __init__(
        self,
        *,
        session: Any,
        cdm: CdmHandle,
        pipeline: str,
        batch_id: str,
        instance_id: str = "",
        event_bus: EventBus | None = None,
        run_date: date | None = None,
        engine: BaseEngine,
        reference_reader: Callable[[str, str], Any] | None = None,
    ) -> None:
        """Create a runner.

        Args:
            session: The engine session tables are read through.
            cdm: Active CDM handle.
            pipeline: Pipeline label.
            batch_id: Batch label (the input's Data Space path).
            instance_id: Run instance id (also the results ``RUN_ID``).
            event_bus: Bus for check lifecycle events.
            run_date: Reference date for future-date checks.
            engine: The compute engine (required). Supplies the column DSL
                and the typed results frame, so the same checks run on
                every engine.
            reference_reader: ``(path, delimiter) -> frame`` loader for
                packaged reference CSVs, built by the caller (the CLI binds
                :func:`ovid2.core.sources.local_file_reader`). The runner
                never constructs a storage driver itself.
        """
        if engine is None:
            raise ValueError(
                "HealthCheckRunner needs an engine; build one through "
                "ovid2.core.loaders.load_engine / the engine registry."
            )
        self.engine = engine
        self.reference_reader = reference_reader
        self.session = session
        self.cdm = cdm
        self.pipeline = pipeline
        self.batch_id = batch_id
        self.instance_id = instance_id
        self.bus = event_bus if event_bus is not None else get_default_bus()
        self.run_date = run_date or date.today()
        self.run_id = instance_id or datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        #: Uppercased table name -> engine DataFrame read by
        #: :meth:`register_input_tables`. Operators resolve tables from here
        #: first, so non-Spark engines need no session temp views.
        self.frames: dict[str, Any] = {}
        #: ``id(frame)`` -> row count memo for the registered base frames.
        #: Most checks start with ``ctx.rows(ctx.table(T))`` on the same few
        #: tables; memoising saves one full scan (one warehouse query on
        #: Snowpark) per repeat.
        self._row_memo: dict[int, int] = {}

    # ------------------------------------------------------------------ #
    # Table registration + availability                                  #
    # ------------------------------------------------------------------ #

    @property
    def _session_is_spark(self) -> bool:
        """Whether the engine reads through the native (legacy address) plane."""
        return bool(getattr(self.engine, "session_is_spark", True))

    def _read_source(self, source: BaseAddress | Driver, table: str) -> Any:
        """Read one table from ``source`` as an engine-native DataFrame.

        Spark-family sessions call ``source.read`` directly (``source`` is a
        legacy address or a driver — both speak the same ``read``
        signature). Non-Spark engines (Snowpark) cannot use the Spark data
        plane, so ``source`` must be a :class:`~ovid2.drivers.base.Driver`
        and the read goes through the engine's stager (native table read
        for Snowflake, ``PUT`` + ``COPY`` for disc, external stages for
        object stores).
        """
        if not self._session_is_spark:
            return self.engine.get_stager().materialise(
                source,
                self.pipeline,
                self.batch_id,
                table,
                table_name=table,
            )
        return source.read(self.session, self.pipeline, self.batch_id, table, table_name=table)

    def register_input_tables(
        self, input_address: BaseAddress | Driver, candidate_tables: list[str]
    ) -> frozenset[str]:
        """Read each candidate CDM table and register it for the operators.

        Every frame read lands in :attr:`frames` (the operators resolve
        tables from there); Spark-family sessions additionally get a temp
        view per table.

        Returns the uppercased set of tables that read successfully — the
        availability set operators consult. A table that fails to read
        (absent file, missing DB object) is simply omitted; ``table_present``
        then reports it missing and required-table checks flag it.

        Raises:
            RuntimeError: When ``candidate_tables`` is non-empty but *none*
                of them could be read. That signals an address-level fault
                (wrong/empty database, unreachable endpoint, bad
                credentials) rather than every individual table happening to
                be absent, so the run fails loudly instead of producing a
                deceptively green summary.
        """
        available: set[str] = set()
        for table in candidate_tables:
            lower = table.lower()
            try:
                df = self._read_source(input_address, lower)
                # Registering the view is lazy; a genuinely absent source
                # raises here or at first use — probe with schema access.
                _ = df.columns  # cheap; forces schema resolution, not data
                self.frames[table.upper()] = df
                if self._session_is_spark:
                    _register_view(table.upper(), df)
                available.add(table.upper())
            except Exception as exc:
                # Carry the real reason (exception type + message) into the
                # warning so an address-level misconfiguration — e.g. a
                # Snowflake address with no database — is diagnosable from the
                # event log instead of surfacing as a generic "not available".
                reason = f"{exc.__class__.__name__}: {exc}"
                logger.warning("health: source table '%s' not available: %s", table, reason)
                self.bus.emit(
                    warning_event(
                        instance_id=self.instance_id,
                        message=(f"health: source table '{table}' not available: {reason}"),
                        table=table,
                        reason=reason,
                    )
                )
        if candidate_tables and not available:
            # Every candidate failed to read. This is almost always an
            # address-level fault (wrong/empty database, unreachable endpoint,
            # bad credentials) rather than 30 independently-missing tables.
            # Fail loudly so the run reports an error instead of a deceptively
            # green summary where only the availability-agnostic checks
            # (e.g. table_present) produce any metrics.
            raise RuntimeError(
                f"0 of {len(candidate_tables)} source tables could be read from "
                f"the input address; check the input address / database "
                f"(pipeline={self.pipeline}, batch_id={self.batch_id})."
            )
        return frozenset(available)

    def register_prior_tables(
        self, prior_address: BaseAddress | Driver, tables: list[str]
    ) -> dict[str, Any]:
        """Read prior-refresh tables for the freshness delta. Best-effort."""
        out: dict[str, Any] = {}
        for table in tables:
            try:
                df = self._read_source(prior_address, table.lower())
                _ = df.columns
                out[table.upper()] = df
            except Exception as exc:
                logger.debug("health: prior table '%s' not available: %s", table, exc)
        return out

    # ------------------------------------------------------------------ #
    # Execution                                                           #
    # ------------------------------------------------------------------ #

    def _count(self, df: Any) -> int:
        """Materialise a row count. The one deliberate action (analysis pass).

        Counts of the registered base frames are memoised (see
        :attr:`_row_memo`); derived frames are always counted afresh.
        """
        key = id(df)
        memo = self._row_memo.get(key)
        if memo is not None:
            return memo
        n = int(df.count())  # noqa: OVID2001 health analysis pass — deliberate
        if any(df is frame for frame in self.frames.values()):
            self._row_memo[key] = n
        return n

    def _read_reference(self, path: str, delimiter: str) -> Any:
        """Load a packaged reference CSV through the caller-supplied reader.

        Raises:
            RuntimeError: When the caller supplied no ``reference_reader``.
        """
        if self.reference_reader is None:
            raise RuntimeError(
                "HealthCheckRunner: this check reads a packaged reference file "
                "but no reference_reader was supplied "
                "(bind ovid2.core.sources.local_file_reader(engine))."
            )
        return self.reference_reader(path, delimiter)

    def _all_empty(
        self,
        ctx: OpContext,
        tables: tuple[str, ...],
        available_tables: frozenset[str],
    ) -> bool:
        """Return ``True`` when *every* table in ``tables`` is empty.

        A table counts as empty when it is absent from the input
        (never read) or read but with zero rows. This mirrors the SAS
        ``_any<table>data`` macro variables that gate the empty-table
        n/a downgrades in ``edc_report.sas`` — e.g. DC_3_08 becomes n/a
        only when ``_anyprescribingdata=0`` (PRESCRIBING absent or empty).

        The check downgrades to n/a only when *all* listed tables are
        empty; if any one is populated the check ran meaningfully and
        keeps its computed status.
        """
        for table in tables:
            upper = table.upper()
            if upper not in available_tables:
                continue  # absent — counts as empty, keep scanning
            if ctx.rows(ctx.table(upper)) > 0:
                return False  # a populated table — not n/a
        return True

    def run(
        self,
        specs: list[CheckSpec],
        *,
        available_tables: frozenset[str],
        prior_tables: dict[str, Any] | None = None,
    ) -> list[CheckOutcome]:
        """Run each spec's operator and return the outcomes (events emitted)."""
        ctx = OpContext(
            session=self.session,
            cdm=self.cdm,
            available_tables=available_tables,
            count_fn=self._count,
            pipeline=self.pipeline,
            run_date=self.run_date,
            prior_tables=prior_tables or {},
            functions=self.engine.get_functions(),
            frames=self.frames,
            reference_reader=self._read_reference,
        )
        outcomes: list[CheckOutcome] = []
        from ovid2.core.pause import gate as _pause_gate

        hold = _pause_gate(
            self.bus,
            instance_id=self.instance_id,
            pipeline=str(self.pipeline or ""),
            batch_id=str(getattr(self, "batch_id", "") or ""),
        )
        for spec in specs:
            # Operator pause holds between checks, never inside one.
            hold()
            outcome = CheckOutcome(
                check_id=spec.id,
                check_name=spec.name,
                part=spec.part,
                edc_table=spec.edc_table,
                description=spec.description,
                doc_interpretation=spec.doc_interpretation,
                severity=spec.severity,
                pack=spec.pack,
            )
            cdm_name = str(getattr(self.cdm, "name", "") or "")
            if cdm_name and not spec.applies_to(cdm_name):
                # Domain-pack scoping: the check is not meaningful for this
                # CDM, so it is reported n/a without running (or reading
                # its tables).
                outcome.status = "not_applicable"
                outcome.skip_reason = (
                    f"applies to {', '.join(spec.applies_to_cdms)}; run CDM is {cdm_name}"
                )
                self.bus.emit(
                    check_completed(
                        instance_id=self.instance_id,
                        check_id=spec.id,
                        name=spec.name,
                        part=spec.part,
                        status=outcome.status,
                    )
                )
                outcomes.append(outcome)
                continue
            self.bus.emit(
                check_started(
                    instance_id=self.instance_id,
                    check_id=spec.id,
                    name=spec.name,
                    part=spec.part,
                )
            )
            op = algorithms.health_operator(spec.operator)
            started = time.monotonic()
            if op is None:
                outcome.status = "error"
                outcome.error = f"unknown operator '{spec.operator}'"
            else:
                try:
                    outcome.rows = op(ctx, spec)
                    outcome.resolve_status()
                    # SAS empty-table -> n/a downgrade
                    # (edc_report.sas:11314-11354). A check declaring
                    # ``na_when_empty`` is reported ``not_applicable`` when
                    # *every* listed source table is absent or has zero
                    # rows — matching the SAS ``_any<table>data=0`` gates.
                    if spec.na_when_empty and self._all_empty(
                        ctx, spec.na_when_empty, available_tables
                    ):
                        outcome.status = "not_applicable"
                except Exception as exc:
                    logger.exception("health check '%s' failed: %s", spec.id, exc)
                    outcome.status = "error"
                    outcome.error = str(exc)
            elapsed_ms = (time.monotonic() - started) * 1000.0
            if outcome.status == "error":
                self.bus.emit(
                    check_failed(
                        instance_id=self.instance_id,
                        check_id=spec.id,
                        name=spec.name,
                        part=spec.part,
                        error=outcome.error or "",
                        elapsed_ms=elapsed_ms,
                    )
                )
            else:
                self.bus.emit(
                    check_completed(
                        instance_id=self.instance_id,
                        check_id=spec.id,
                        name=spec.name,
                        part=spec.part,
                        status=outcome.status,
                        exception_count=outcome.exception_count,
                        metric_count=len(outcome.rows),
                        elapsed_ms=elapsed_ms,
                    )
                )
            outcomes.append(outcome)
        return outcomes

    # ------------------------------------------------------------------ #
    # Durable results table                                              #
    # ------------------------------------------------------------------ #

    def build_results_dataframe(self, outcomes: list[CheckOutcome]) -> Any:
        """Return a lazy DataFrame of every metric row (DCQ_RESULTS shape).

        This is written to the output storage account by the caller. All
        the values are already Python scalars (the operators materialised
        their counts), so building the frame is a plain ``createDataFrame``
        — no further Spark action is triggered until the write.

        An explicit schema is supplied so nullable numeric / detail columns
        infer correctly even when a whole column is ``None`` for a run.
        """
        rows: list[tuple[Any, ...]] = []
        for outcome in outcomes:
            for metric in outcome.rows:
                rows.append(metric.as_table_row(self.run_id))
        return self.engine.create_typed_dataframe(rows, RESULTS_SCHEMA)


def all_candidate_tables(specs: list[CheckSpec], cdm_name: str | None = None) -> list[str]:
    """Return the distinct source tables referenced across ``specs``.

    When ``cdm_name`` is given, specs that do not apply to that CDM
    (``applies_to_cdms``) contribute no tables — they are reported
    ``not_applicable`` without reading anything.
    """
    seen: dict[str, None] = {}
    for spec in specs:
        if cdm_name is not None and not spec.applies_to(cdm_name):
            continue
        for table in spec.source_tables:
            seen.setdefault(table.upper(), None)
        # orphan_fk / freshness reference tables in params too.
        for rel in spec.params.get("relationships", []):
            seen.setdefault(str(rel.get("child", "")).upper(), None)
            seen.setdefault(str(rel.get("parent", "")).upper(), None)
    seen.pop("", None)
    return list(seen)


def _register_view(name: str, df: Any) -> None:
    """Best-effort register ``df`` as a temp view under ``name``.

    Kept for SQL-level consumers on the Spark family (the operators
    themselves resolve tables from :attr:`HealthCheckRunner.frames`). A
    failure is logged, never raised: the frame registry is authoritative.
    """
    for method in ("createOrReplaceTempView", "create_or_replace_temp_view"):
        create = getattr(df, method, None)
        if callable(create):
            try:
                create(name)
            except Exception as exc:
                logger.debug("health: temp view %s not registered: %s", name, exc)
            return


#: Column layout of the durable ``DCQ_RESULTS`` metric-row table, as
#: ``(name, portable_type)`` pairs for :meth:`BaseEngine.create_typed_dataframe`.
#: An explicit schema avoids type-inference failures when a nullable numeric /
#: string column is ``None`` for an entire run. Positionally aligned with
#: :meth:`~ovid2.transformations.quality.results.MetricRow.as_table_row`.
RESULTS_SCHEMA: list[tuple[str, str]] = [
    ("RUN_ID", "string"),
    ("CHECK_ID", "string"),
    ("CHECK_NAME", "string"),
    ("PART", "string"),
    ("EDC_TABLE", "string"),
    ("SOURCE_TABLE", "string"),
    ("METRIC", "string"),
    ("VALUE_NUM", "double"),
    ("VALUE_STR", "string"),
    ("THRESHOLD_NUM", "double"),
    ("EXCEPTION_FLAG", "boolean"),
    ("DETAILS", "string"),
]


def select_health_specs(
    parts: list[str] | None,
    checks: list[str] | None,
    packages: list[str] | None = None,
) -> list[CheckSpec]:
    """Thin wrapper over the registry selector for the CLI."""
    return algorithms.select_health_checks(parts=parts, checks=checks, packages=packages)


__all__ = [
    "RESULTS_SCHEMA",
    "HealthCheckRunner",
    "all_candidate_tables",
    "select_health_specs",
]
