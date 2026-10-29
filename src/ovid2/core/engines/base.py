"""Base compute-engine interface.

An engine wraps whichever query/execution context the pipeline needs
(Spark session, Databricks-Connect client, Snowpark connection). It has
three responsibilities:

1. Hand out a session (:meth:`get_session`).
2. Expose the column-expression DSL (:attr:`functions` /
   :meth:`get_functions`) — the pipeline never imports
   ``pyspark.sql.functions`` directly.
3. Advertise its :class:`EngineFamily` and capabilities so fixers /
   adapters can gate themselves.

Concrete implementations live under :mod:`ovid2.core.engines`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC
from typing import TYPE_CHECKING, Any

from ovid2.core.engines.protocols import EngineFamily, EngineFunctions, EngineSession

if TYPE_CHECKING:
    from ovid2.core.engines.metrics import EngineMetricsSample

# Deliberately imported lazily inside :meth:`BaseEngine.get_stager` so
# this module stays free of heavy dependencies at import time. The
# stager module itself is import-safe (no pyspark, no snowpark), but
# routing the import through ``get_stager`` keeps the surface of the
# base class small and forgiving.


@dataclass(frozen=True)
class EngineTestResult:
    """Outcome of :meth:`BaseEngine.test`.

    Attributes:
        ok: Whether the probe succeeded.
        code: One of ``ok``, ``unreachable``, ``auth``, ``timeout``,
            ``not_implemented``, ``skipped``, ``error``.
        message: Human-readable summary.
        detail: Additional structured detail.
    """

    ok: bool
    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


class BaseEngine(ABC):
    """Abstract base class for compute engines.

    Every concrete engine sets :attr:`type_name` (matching the
    registry key) and :attr:`family`. Non-Spark engines set the
    ``supports_*`` capability flags to :data:`False`; the pipeline
    consults these flags before running Spark-only operations
    (broadcast join hints, ``input_file_name()``, ``pandas_udf`` etc.).
    """

    #: Registry key (``pyspark``, ``databricks_spark``, ``snowpark``).
    type_name: str = ""
    #: Broad compute family. Determines which fixers can run.
    family: EngineFamily = EngineFamily.SPARK

    # ---- Capability flags (Spark defaults; Snowpark overrides) -----------
    supports_pandas_udf: bool = True
    supports_broadcast_hint: bool = True
    supports_input_file_name: bool = True
    supports_local_files: bool = True
    #: True if the engine's session is API-compatible with a real
    #: ``pyspark.sql.SparkSession``. Databricks Connect satisfies this;
    #: Snowpark does not.
    session_is_spark: bool = True

    def __init__(
        self,
        args: dict[str, Any] | None = None,
        secrets: dict[str, Any] | None = None,
    ) -> None:
        """Store non-secret configuration and (redacted) secrets."""
        self.args: dict[str, Any] = dict(args or {})
        self.secrets: dict[str, Any] = dict(secrets or {})

    @abstractmethod
    def get_session(self) -> EngineSession:
        """Return an active session, creating one lazily if needed."""

    @abstractmethod
    def stop_session(self) -> None:
        """Dispose of the active session, if any."""

    def get_functions(self) -> EngineFunctions:
        """Return the engine's column-expression DSL.

        Default implementation raises :class:`NotImplementedError`;
        concrete engines override.
        """
        raise NotImplementedError(
            f"Engine '{self.type_name}' does not expose a functions namespace"
        )

    def native(self) -> Any:
        """Return the underlying native session (escape hatch).

        Used by pipeline code and Spark-family-only fixers that need
        access to the real ``SparkSession`` / ``snowpark.Session``.
        Callers that use this MUST declare
        ``requires_family="spark"`` (or the matching family) in their
        registry entry so they never run under an incompatible engine.
        """
        return self.get_session()

    def test(self) -> EngineTestResult:
        """Probe the engine.

        Default returns ``not_implemented``. Concrete engines override
        with a cheap connectivity / session-build check.
        """
        return EngineTestResult(
            ok=False,
            code="not_implemented",
            message=f"Engine '{self.type_name}' has no test probe",
        )

    def get_metrics(self) -> EngineMetricsSample:
        """Return a cheap point-in-time metrics sample.

        Called on a fixed cadence by
        :func:`ovid2.core.metrics_sampler.run_sampler` while a run is
        in flight. The default implementation returns a bare sample
        with only ``engine_family`` populated — enough for the UI's
        pulse to show "engine is alive" without any per-engine
        detail. Concrete engines override with a real snapshot.

        This method MUST be:

        - Cheap. Metadata-only calls; never triggers a job.
        - Non-throwing under normal conditions. The sampler catches
          exceptions defensively but callers should not rely on it.
        - Free of ``.collect()`` / ``.toPandas()`` — enforced by
          ``tests/contract/test_no_collect_in_src.py``.
        """
        # Local import to keep ``base.py`` free of the (very cheap)
        # metrics module until someone actually samples.
        import time
        from datetime import datetime

        from ovid2.core.engines.metrics import EngineMetricsSample

        family_value = (
            self.family.value if isinstance(self.family, EngineFamily) else str(self.family)
        )
        return EngineMetricsSample(
            t_ms=int(time.monotonic() * 1000),
            wall_iso=datetime.now(UTC).isoformat(),
            engine_family=family_value,
        )

    def cancel_running_queries(self) -> None:
        """Best-effort cancel of any in-flight compute for this engine.

        Invoked from the ETL entry point's SIGTERM / SIGINT handler when
        an operator cancels a run from the web panel (the worker delivers
        a cooperative SIGTERM to the host-side ``ovid2`` process, which
        propagates into the container). The default is a no-op: for the
        Spark family, tearing the driver / session down already stops the
        DAG, so there is no separate per-query cancel to issue.
        :class:`~ovid2.core.engines.snowpark.SnowparkEngine` overrides
        this to abort the queries running on its live Snowflake session.

        MUST be non-throwing — the caller is already unwinding on a
        signal and must reach the ``finally`` teardown regardless.
        """
        return None

    # ---- DataFrame-shape portable helpers -------------------------------
    #
    # A handful of DataFrame methods diverge in name / signature between
    # PySpark and Snowpark (``unionByName`` vs ``union_by_name``,
    # ``dropDuplicates`` vs ``drop_duplicates``, ``withColumn`` vs
    # ``with_column``). Engine-portable pipeline formatters call these
    # helpers instead of the raw method so they never branch on the
    # engine. The default implementations here assume the PySpark
    # (camelCase) API; :class:`~ovid2.core.engines.snowpark.SnowparkEngine`
    # overrides them with the snake_case Snowpark equivalents.

    def union_by_name(self, left: Any, right: Any, *, allow_missing_columns: bool = False) -> Any:
        """Union two DataFrames by column name.

        Spark: ``left.unionByName(right, allowMissingColumns=...)``.
        Snowpark's ``union_by_name`` has no ``allow_missing_columns``
        kwarg, so :class:`SnowparkEngine` overrides this to align
        schemas first when ``allow_missing_columns`` is set.
        """
        return left.unionByName(right, allowMissingColumns=allow_missing_columns)

    def drop_duplicates(self, df: Any, cols: list[str] | None = None) -> Any:
        """Drop duplicate rows, optionally scoped to ``cols``.

        Spark: ``df.dropDuplicates(cols)``. Snowpark: ``df.drop_duplicates``.
        """
        if cols is None:
            return df.dropDuplicates()
        return df.dropDuplicates(cols)

    def with_column(self, df: Any, name: str, col: Any) -> Any:
        """Add / replace a column.

        Spark: ``df.withColumn(name, col)``. Snowpark: ``with_column``.
        """
        return df.withColumn(name, col)

    def checkpoint(self, df: Any, name: str) -> Any:
        """Materialise ``df`` once so downstream reads re-scan a cache.

        A *checkpoint* breaks a large lazy plan into two: the DataFrame
        is computed a single time and every downstream consumer reads the
        pre-computed result instead of re-deriving the whole upstream
        graph. This is the portable analogue of Spark's ``.cache()`` and
        Snowpark's transient-table materialisation, used by formatters
        whose output is consumed by several branches (e.g. the
        PCORnet->OMOP ``common`` fan-in, which three domain formatters
        each resolve concepts against, and which itself unions 5 sources
        with an 8-way VITAL fan-out — without a checkpoint that whole
        graph is re-derived inside every domain's final CTAS).

        The Spark default marks the DataFrame for caching and returns it
        (lazy — the cache fills on first action). :class:`SnowparkEngine`
        overrides this to write ``df`` to a transient table and return a
        fresh ``session.table(...)`` handle onto it, so the multi-source
        union is executed once server-side and each downstream domain
        scans the small transient table.

        Args:
            df: The DataFrame to materialise.
            name: A caller-supplied logical name (used to derive the
                transient-table name on engines that persist; ignored by
                the caching default). Should be unique within a run.

        Returns:
            A DataFrame equivalent to ``df`` but backed by a
            materialisation boundary. Callers must use the returned
            handle, not the original ``df``.
        """
        del name  # unused by the caching default
        return df.cache()

    def get_stager(self) -> Any:
        """Return the :class:`EngineStager` for this engine's family.

        The stager owns the data-plane bridge between an
        :class:`EngineSession` and any :class:`~ovid2.drivers.base.Driver`.
        Spark-family engines get a pure pass-through
        (:class:`~ovid2.core.engines.stager.SparkEngineStager`);
        Snowpark gets a dispatch-on-driver-type stager
        (:class:`~ovid2.core.engines.stager.SnowparkEngineStager`).

        The stager is created fresh on every call. It's a very light
        object (holds a back-reference to the engine and nothing else),
        so callers that need one per table are welcome to make repeated
        calls; :meth:`EngineStager.session` still routes through the
        engine's cached session.

        Raises:
            RuntimeError: When the engine's family has no registered
                stager class. Every family listed in
                :class:`EngineFamily` is registered today, so this is
                a should-not-happen guard.
        """
        # Local import — the stager module is import-safe but living
        # under ``ovid2.core.engines`` we prefer to keep ``base.py``
        # importable even if the stager module is being rewritten.
        from ovid2.core.engines.stager import stager_class_for_engine_family

        family_value = (
            self.family.value if isinstance(self.family, EngineFamily) else str(self.family)
        )
        stager_cls = stager_class_for_engine_family(family_value)
        if stager_cls is None:
            raise RuntimeError(
                f"Engine '{self.type_name}' declares family "
                f"'{family_value}' which has no registered stager. "
                f"Add an entry to "
                f"ovid2.core.engines.stager._FAMILY_TO_STAGER."
            )
        return stager_cls(self)

    def create_typed_dataframe(
        self,
        rows: list[tuple[Any, ...]],
        schema: list[tuple[str, str]],
    ) -> Any:
        """Build an engine-native DataFrame from Python rows + a portable schema.

        Engine-portable callers (the data-health results table, for one)
        describe column types with the portable names ``string`` /
        ``double`` / ``boolean`` / ``long`` / ``date`` / ``timestamp``
        instead of importing ``pyspark.sql.types`` or
        ``snowflake.snowpark.types`` themselves. Every column is nullable,
        so an all-``None`` column never trips type inference.

        The default implementation serves the Spark family (``pyspark`` and
        ``databricks_spark`` share it); :class:`SnowparkEngine` overrides.

        Args:
            rows: Row tuples, positionally aligned with ``schema``.
            schema: ``(column_name, portable_type)`` pairs.

        Returns:
            A lazy engine-native DataFrame.
        """
        from ovid2.core.engines.pyspark import create_typed_dataframe

        return create_typed_dataframe(self.get_session(), rows, schema)

    def prepare_workspace(self, fallback_databases: Sequence[str] = ()) -> None:
        """Bind a writable namespace for engine-managed temporary objects.

        The pipeline registers intermediate results as temporary views
        (``createOrReplaceTempView`` / ``create_or_replace_temp_view``)
        so cross-table fixers and concept joins can reference them by
        name. On Spark these live in the session-scoped in-memory
        catalogue, so no preparation is needed and this is a no-op.

        On engines where temporary objects are created *inside the
        backing database* (Snowpark: temp views land in the session's
        current database + schema), this hook must ensure the session's
        current namespace is writable — otherwise the temp view creation
        fails (e.g. Snowflake's "Creating view on shared database
        'SNOWFLAKE' is not allowed" when the session defaulted to the
        read-only system database). Called once per run, before any
        temp view is registered.

        Args:
            fallback_databases: Candidate databases (typically the run's
                warehouse output / input addresses, output first) the
                engine may adopt for its scratch namespace when its own
                configuration names none. Ignored by the Spark family.
                Only ever used for engine scratch objects — never as a
                write target, so the "address is the exact location"
                rule is unaffected.
        """
        del fallback_databases
        return None


__all__ = ["BaseEngine", "EngineTestResult"]
