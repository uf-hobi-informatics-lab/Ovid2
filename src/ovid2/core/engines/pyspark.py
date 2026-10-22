"""PySpark compute engine.

Wraps a real :class:`pyspark.sql.SparkSession`. This is the default
engine (matches the pre-E1 behaviour byte-for-byte when instantiated
with no args) and also the only engine that may import from
``pyspark.sql.functions`` / ``pyspark.sql.types``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import TYPE_CHECKING, Any

from ovid2.core.engines.base import BaseEngine, EngineTestResult
from ovid2.core.engines.protocols import EngineFamily, EngineFunctions, EngineSession

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

    from ovid2.core.engines.metrics import EngineMetricsSample


#: Maximum number of bytes of captured JVM stderr to surface in
#: ``EngineTestResult.detail['stderr_tail']``. Kept small so a runaway
#: JVM stack trace does not blow past the JSON contract's practical size.
_STDERR_TAIL_BYTES: int = 2048


def _locate_jdk() -> str | None:
    """Return a plausible JDK location, or ``None`` if none is visible.

    Checks ``JAVA_HOME`` first (what PySpark itself consults), then falls
    back to ``java`` on ``PATH``. Does **not** validate the JDK actually
    works — just that *something* claims to be there. Genuine JVM
    failures (wrong arch, missing ``libjvm.so``) are caught later by
    the exception mapping in :meth:`PySparkEngine.test`.
    """
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        return java_home
    which_java = shutil.which("java")
    if which_java:
        return which_java
    return None


def _missing_jdk_result(extra_detail: dict[str, Any] | None = None) -> EngineTestResult:
    """Build the standard ``missing_jdk`` :class:`EngineTestResult`."""
    detail: dict[str, Any] = {
        "hint": (
            "Install a JDK (e.g. `sudo apt install openjdk-17-jre-headless`) "
            "and either put `java` on PATH or set JAVA_HOME. After installing, "
            "restart the ovid2 web service so the new PATH is picked up."
        ),
    }
    if extra_detail:
        detail.update(extra_detail)
    return EngineTestResult(
        ok=False,
        code="missing_jdk",
        message=(
            "No JDK found: `java` is not on PATH and JAVA_HOME is unset. "
            "PySpark requires a JDK to launch its JVM gateway."
        ),
        detail=detail,
    )


class _SparkFunctions:
    """Adapter exposing ``pyspark.sql.functions`` as :class:`EngineFunctions`.

    Every method is a thin pass-through so PySpark and Databricks-Connect
    consumers pay no wrapping overhead. Instances of this class are
    cached on the engine.
    """

    def __init__(self) -> None:
        # Import lazily so callers that never touch functions (installer,
        # web tier when constructing an engine class object for
        # introspection) do not force a pyspark dependency.
        from pyspark.sql import functions as F

        self._F = F

    def col(self, name: str) -> Any:
        return self._F.col(name)

    def lit(self, value: Any) -> Any:
        return self._F.lit(value)

    def when(self, cond: Any, value: Any) -> Any:
        return self._F.when(cond, value)

    def coalesce(self, *cols: Any) -> Any:
        return self._F.coalesce(*cols)

    def upper(self, c: Any) -> Any:
        return self._F.upper(c)

    def lower(self, c: Any) -> Any:
        return self._F.lower(c)

    def trim(self, c: Any) -> Any:
        return self._F.trim(c)

    def substring(self, c: Any, pos: int, length: int) -> Any:
        return self._F.substring(c, pos, length)

    def regexp_replace(self, c: Any, pattern: str, replacement: str) -> Any:
        return self._F.regexp_replace(c, pattern, replacement)

    def regexp_extract(self, c: Any, pattern: str, idx: int) -> Any:
        return self._F.regexp_extract(c, pattern, idx)

    def to_date(self, c: Any, fmt: str | None = None) -> Any:
        if fmt is None:
            return self._F.to_date(c)
        return self._F.to_date(c, fmt)

    def to_timestamp(self, c: Any, fmt: str | None = None) -> Any:
        if fmt is None:
            return self._F.to_timestamp(c)
        return self._F.to_timestamp(c, fmt)

    def date_format(self, c: Any, fmt: str) -> Any:
        return self._F.date_format(c, fmt)

    def datediff(self, end: Any, start: Any) -> Any:
        return self._F.datediff(end, start)

    def current_timestamp(self) -> Any:
        return self._F.current_timestamp()

    def count(self, c: Any) -> Any:
        return self._F.count(c)  # noqa: OVID2001 aggregate function reference, not an eager action

    def broadcast(self, df: Any) -> Any:
        return self._F.broadcast(df)

    def input_file_name(self) -> Any:
        return self._F.input_file_name()

    def expr(self, sql_string: str) -> Any:
        return self._F.expr(sql_string)

    # --- Portable extensions (E-portability phase 1) --------------------

    def concat(self, *cols: Any) -> Any:
        return self._F.concat(*cols)

    def concat_ws(self, sep: str, *cols: Any) -> Any:
        return self._F.concat_ws(sep, *cols)

    def current_date(self) -> Any:
        return self._F.current_date()

    def max(self, c: Any) -> Any:
        return self._F.max(c)  # noqa: OVID2001 aggregate function reference, not an eager action

    def map_from_dict(
        self, mapping: dict[str, Any], key_col: Any, default: Any = None
    ) -> Any:
        """Emit a chained ``when(...).otherwise(...)`` mapping.

        Portable replacement for the Spark ``create_map(...)[key]``
        indexing idiom, which has no Snowpark analog (Snowpark ``Column``
        has no ``__getitem__`` over a map literal). Both engines produce
        an equivalent CASE expression, so pipeline code never branches.

        Args:
            mapping: ``{source_value: mapped_value}``.
            key_col: The ``Column`` whose value is looked up.
            default: Value used when ``key_col`` matches no key (``None``
                emits a bare ``NULL`` fallback).

        Returns:
            A ``Column`` expressing the lookup.
        """
        if not mapping:
            return self._F.lit(default)
        items = iter(mapping.items())
        first_key, first_val = next(items)
        expr = self._F.when(key_col == self._F.lit(first_key), self._F.lit(first_val))
        for key, val in items:
            expr = expr.when(key_col == self._F.lit(key), self._F.lit(val))
        return expr.otherwise(self._F.lit(default))

    def stable_hash(self, *cols: Any) -> Any:
        """Return a deterministic non-negative 63-bit integer as a string.

        Uses Spark's 64-bit ``xxhash64`` masked to clear the sign bit so
        the value is always ``>= 0``, rendered as a decimal string
        (OMOP surrogate ids are ``RDBMS Number``). Null / empty
        components are coalesced to the empty string so a missing part
        still yields a stable id.
        """
        parts = [self._F.coalesce(c.cast("string"), self._F.lit("")) for c in cols]
        return (
            self._F.xxhash64(*parts).bitwiseAND(self._F.lit(0x7FFF_FFFF_FFFF_FFFF))
        ).cast("string")

    def rand(self, seed: int | None = None) -> Any:
        if seed is None:
            return self._F.rand()
        return self._F.rand(seed=seed)


class PySparkEngine(BaseEngine):
    """Concrete :class:`BaseEngine` backed by a single ``SparkSession``.

    Configuration precedence:

    1. Built-in :data:`ovid2.config.settings.SPARK_CONFIG` defaults.
    2. Per-engine ``args`` passed at construction time
       (from ``$XDG_CONFIG_HOME/ovid2/engines/<name>.json``). Any
       string key is layered on top and used as-is. Unknown keys are
       forwarded to Spark; Spark itself will reject typos.

    The session is created on first :meth:`get_session` call and reused
    for the rest of the process. Logs are lowered to ``OFF`` after the
    session comes up (loud Spark logs are handled by the log4j
    properties bundled in ``resources/``).
    """

    type_name = "pyspark"
    family = EngineFamily.SPARK
    session_is_spark = True

    def __init__(
        self,
        args: dict[str, Any] | None = None,
        secrets: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(args=args, secrets=secrets)
        self._session: SparkSession | None = None
        self._functions: EngineFunctions | None = None

    def get_session(self) -> EngineSession:
        """Return the singleton Spark session, creating it if needed."""
        if self._session is None:
            # Lazy import — the pyspark module isn't available on host-side
            # tooling paths (installer, cli --help, etc.).
            from pyspark.sql import SparkSession

            from ovid2.config.settings import SPARK_CONFIG

            app_name = str(self.args.get("app_name") or "Ovid2 ETL")
            builder = SparkSession.builder.appName(app_name)

            # Layer built-in defaults, then per-engine args on top.
            merged: dict[str, str] = {}
            for key, value in SPARK_CONFIG.items():
                merged[key] = str(value)
            for key, value in self.args.items():
                if key in ("app_name", "master_url"):
                    continue
                merged[key] = str(value)

            master_url = self.args.get("master_url")
            if not master_url:
                # Env-var fallback used by the host-side cluster runner: when
                # ``ovid2 etl`` is launched in ``SPARK_MODE=cluster`` the
                # driver container is started with ``SPARK_MASTER_URL`` set
                # to the isolated cluster's ``spark://…:7077``. Without this
                # fallback the driver would silently run ``local[*]`` and
                # the 25 workers would sit idle.
                import os as _os

                master_url = _os.environ.get("SPARK_MASTER_URL") or None
            if master_url:
                builder = builder.master(str(master_url))

            for key, value in merged.items():
                builder = builder.config(key, value)
            self._session = builder.getOrCreate()
            self._session.sparkContext.setLogLevel("OFF")
        # Structural-typing: SparkSession satisfies EngineSession.
        return self._session  # type: ignore[return-value]

    def stop_session(self) -> None:
        """Stop the Spark session if one has been created."""
        if self._session is not None:
            self._session.stop()
            self._session = None
            self._functions = None

    def get_functions(self) -> EngineFunctions:
        """Return the cached :class:`EngineFunctions` adapter."""
        if self._functions is None:
            self._functions = _SparkFunctions()
        return self._functions

    def native(self) -> SparkSession:
        """Return the underlying real ``SparkSession``."""
        session = self.get_session()
        return session  # type: ignore[return-value]

    def test(self) -> EngineTestResult:
        """Build a session, run ``SELECT 1``, and tear it down cleanly.

        Runs three checks in order, each producing a distinct result
        ``code`` so the operator can act on the failure:

        1. **``unreachable``** — pyspark itself is not importable.
        2. **``missing_jdk``** — no JDK is visible via ``JAVA_HOME`` or
           ``PATH``. Bailed out *before* trying to launch a JVM so the
           user never sees the opaque py4j ``Java gateway process
           exited before sending its port number`` message.
        3. **``missing_jdk`` (post-launch)** — a JDK was on ``PATH`` but
           launching it produced the py4j gateway error anyway (wrong
           architecture, missing ``libjvm.so``, corrupt install). The
           JVM's own stderr is captured into ``detail['stderr_tail']``.
        4. **``error``** — any other failure. Captures the JVM's stderr
           tail into ``detail['stderr_tail']`` when available.
        5. **``ok``** — ``SELECT 1`` returned 1.
        """
        try:
            from pyspark.sql import SparkSession  # noqa: F401
        except ImportError as exc:
            return EngineTestResult(
                ok=False,
                code="unreachable",
                message=f"pyspark not importable: {exc}",
            )

        # Preflight: refuse to invoke SparkSession.builder if there is
        # obviously no JVM to launch. Faster feedback, cleaner error.
        jdk_location = _locate_jdk()
        if jdk_location is None:
            return _missing_jdk_result()

        # Redirect low-level stderr (fd 2) for the duration of session
        # build so any JVM crash output is preserved. We must operate on
        # the OS-level file descriptor because py4j spawns a child
        # process whose stderr is inherited from fd 2, not from
        # ``sys.stderr`` (which the child never sees).
        stderr_tail: str = ""
        saved_stderr_fd: int | None = None
        captured_path: str | None = None
        try:
            try:
                saved_stderr_fd = os.dup(2)
                tmp = tempfile.NamedTemporaryFile(
                    prefix="ovid2-engine-probe-",
                    suffix=".stderr",
                    delete=False,
                )
                captured_path = tmp.name
                tmp_fd = tmp.file.fileno()
                os.dup2(tmp_fd, 2)
                tmp.close()
            except OSError:
                # Redirection is best-effort; if it fails, run without.
                if saved_stderr_fd is not None:
                    try:
                        os.close(saved_stderr_fd)
                    except OSError:
                        pass
                saved_stderr_fd = None
                captured_path = None

            try:
                session = self.get_session()
                row = session.sql("SELECT 1 AS ok").collect()  # noqa: OVID2001
            finally:
                if saved_stderr_fd is not None:
                    try:
                        os.dup2(saved_stderr_fd, 2)
                    except OSError:
                        pass
                    try:
                        os.close(saved_stderr_fd)
                    except OSError:
                        pass
                if captured_path is not None:
                    try:
                        with open(captured_path, "rb") as fh:
                            fh.seek(0, os.SEEK_END)
                            size = fh.tell()
                            fh.seek(max(0, size - _STDERR_TAIL_BYTES))
                            stderr_tail = fh.read().decode("utf-8", errors="replace")
                    except OSError:
                        stderr_tail = ""
                    try:
                        os.unlink(captured_path)
                    except OSError:
                        pass

            if row and row[0][0] == 1:
                return EngineTestResult(
                    ok=True, code="ok", message="Spark session built and responded to SELECT 1"
                )
            return EngineTestResult(
                ok=False,
                code="error",
                message="SELECT 1 returned unexpected result",
                detail={"row": str(row)},
            )
        except Exception as exc:  # noqa: BLE001
            exc_message = str(exc)
            detail: dict[str, Any] = {
                "exception_type": type(exc).__name__,
                "exception_message": exc_message,
            }
            if stderr_tail:
                detail["stderr_tail"] = stderr_tail
            # py4j surfaces a missing / broken JVM as a plain
            # ``RuntimeError`` with this exact substring. Map it to the
            # dedicated missing_jdk code with an actionable hint even
            # though ``java`` was on PATH — usually means wrong arch or
            # a broken openjdk-headless install.
            if isinstance(exc, RuntimeError) and "Java gateway" in exc_message:
                detail["hint"] = (
                    "`java` was found at "
                    f"{jdk_location!r} but the JVM failed to start. "
                    "Check `java -version` runs cleanly; try reinstalling "
                    "openjdk-17-jre-headless or set JAVA_HOME to a known-good JDK."
                )
                return EngineTestResult(
                    ok=False,
                    code="missing_jdk",
                    message=f"Spark probe failed: {exc}",
                    detail=detail,
                )
            return EngineTestResult(
                ok=False,
                code="error",
                message=f"Spark probe failed: {exc}",
                detail=detail,
            )

    # ---------------------------------------------------------- metrics
    #
    # ``get_metrics`` is called on a fixed cadence (500 ms default) by the
    # metrics sampler while an ETL is in flight. It reads a handful of
    # cheap in-JVM ``StatusTracker`` fields plus optional per-task
    # accumulators fed by a small ``SparkListener``. Never triggers a job.

    def _ensure_metrics_listener(self) -> None:
        """Attach a ``SparkListener`` that accumulates rows written.

        Called lazily the first time :meth:`get_metrics` runs; a no-op
        after that. The listener lives inside the JVM and accumulates
        ``taskEnd.taskMetrics.outputMetrics.recordsWritten`` into a
        Python-visible counter. Failure to attach the listener (older
        Spark, py4j quirks) is *not* an error — we just fall back to
        the bytes-written figure that ``StatusTracker`` exposes
        natively.
        """
        if getattr(self, "_metrics_listener_attached", False):
            return
        # Mark first so a broken listener path doesn't retry forever.
        self._metrics_listener_attached = True
        self._rows_written_counter: int = 0

        try:
            if self._session is None:
                return
            sc = self._session.sparkContext
            jvm = sc._jvm  # type: ignore[attr-defined]
            if jvm is None:  # pragma: no cover — defensive
                return

            # Build a Python-side listener class implementing the
            # SparkListener interface via py4j's ``implements``.
            engine = self

            class _OvidMetricsListener:  # pragma: no cover — JVM-side callback
                """Row-count accumulator wired into Spark's listener bus."""

                def onTaskEnd(self, taskEnd: Any) -> None:  # noqa: N802,N803
                    try:
                        tm = taskEnd.taskMetrics()
                        if tm is None:
                            return
                        om = tm.outputMetrics()
                        if om is None:
                            return
                        engine._rows_written_counter += int(om.recordsWritten())
                    except Exception:
                        # Never let a listener bug surface to Spark.
                        pass

                # These methods are optional but must exist for py4j
                # ``implements`` to satisfy the interface.
                def onStageCompleted(self, _e: Any) -> None: pass  # noqa: N802, E704
                def onStageSubmitted(self, _e: Any) -> None: pass  # noqa: N802, E704
                def onTaskStart(self, _e: Any) -> None: pass  # noqa: N802, E704
                def onTaskGettingResult(self, _e: Any) -> None: pass  # noqa: N802, E704
                def onJobStart(self, _e: Any) -> None: pass  # noqa: N802, E704
                def onJobEnd(self, _e: Any) -> None: pass  # noqa: N802, E704
                def onEnvironmentUpdate(self, _e: Any) -> None: pass  # noqa: N802, E704
                def onBlockManagerAdded(self, _e: Any) -> None: pass  # noqa: N802, E704
                def onBlockManagerRemoved(self, _e: Any) -> None: pass  # noqa: N802, E704
                def onUnpersistRDD(self, _e: Any) -> None: pass  # noqa: N802, E704
                def onApplicationStart(self, _e: Any) -> None: pass  # noqa: N802, E704
                def onApplicationEnd(self, _e: Any) -> None: pass  # noqa: N802, E704
                def onExecutorMetricsUpdate(self, _e: Any) -> None: pass  # noqa: N802, E704
                def onExecutorAdded(self, _e: Any) -> None: pass  # noqa: N802, E704
                def onExecutorRemoved(self, _e: Any) -> None: pass  # noqa: N802, E704
                def onExecutorBlacklisted(self, _e: Any) -> None: pass  # noqa: N802, E704
                def onExecutorUnblacklisted(self, _e: Any) -> None: pass  # noqa: N802, E704
                def onNodeBlacklisted(self, _e: Any) -> None: pass  # noqa: N802, E704
                def onNodeUnblacklisted(self, _e: Any) -> None: pass  # noqa: N802, E704
                def onBlockUpdated(self, _e: Any) -> None: pass  # noqa: N802, E704
                def onSpeculativeTaskSubmitted(self, _e: Any) -> None: pass  # noqa: N802, E704
                def onOtherEvent(self, _e: Any) -> None: pass  # noqa: N802, E704

                class Java:
                    implements = ["org.apache.spark.scheduler.SparkListenerInterface"]

            self._metrics_listener_impl = _OvidMetricsListener()
            sc._jsc.sc().addSparkListener(self._metrics_listener_impl)  # type: ignore[attr-defined]
        except Exception:
            # Listener attach is best-effort. Metrics still work without
            # rows_written; the tile just reads "—" for rows.
            self._metrics_listener_impl = None

    def get_metrics(self) -> "EngineMetricsSample":
        """Read a cheap live snapshot from the Spark ``StatusTracker``.

        Returns an :class:`EngineMetricsSample` with:

        - executor active/total counts
        - active-task count summed across active stages
        - cumulative completed / failed task counts
        - a stage-completion counter (used for the ETA heuristic)
        - a friendly current-stage name (newest active stage)
        - cumulative input / output bytes
        - cumulative rows written (when the listener is attached)

        All values are ``None`` when the session isn't up yet — the
        sampler starts before the driver's first job, so early samples
        are legitimately blank.
        """
        # Local imports so this module still parses under mypy on hosts
        # without pyspark. Imports themselves are cached by CPython so
        # this is not a per-call cost after the first sample.
        import time
        from datetime import datetime, timezone

        from ovid2.core.engines.metrics import EngineMetricsSample

        now_ms = int(time.monotonic() * 1000)
        wall = datetime.now(timezone.utc).isoformat()

        # No session yet → return a bare "alive" sample so the pulse
        # can still show engine_family. Not an error.
        if self._session is None:
            return EngineMetricsSample(
                t_ms=now_ms, wall_iso=wall, engine_family=self.family.value
            )

        try:
            self._ensure_metrics_listener()
            sc = self._session.sparkContext
            tracker = sc.statusTracker()

            active_stage_ids = list(tracker.getActiveStageIds())
            active_stage_infos = [
                info
                for info in (tracker.getStageInfo(sid) for sid in active_stage_ids)
                if info is not None
            ]
            tasks_active = sum(int(info.numActiveTasks()) for info in active_stage_infos)
            tasks_completed = sum(
                int(info.numCompletedTasks()) for info in active_stage_infos
            )
            tasks_failed = sum(
                int(info.numFailedTasks()) for info in active_stage_infos
            )
            current_stage_name: str | None = None
            if active_stage_infos:
                # Newest active stage wins — most recently-submitted is
                # what the operator actually sees "cooking" right now.
                current_stage_name = str(active_stage_infos[-1].name())

            executor_infos = list(tracker.getExecutorInfos())
            executors_total = len(executor_infos)
            executors_active = sum(
                1 for e in executor_infos if int(e.numRunningTasks()) > 0
            )

            # Cumulative input/output bytes are per-stage on the
            # tracker; sum across the whole run.
            input_bytes = 0
            output_bytes = 0
            for info in active_stage_infos:
                try:
                    input_bytes += int(info.numInputBytes())
                    output_bytes += int(info.numOutputBytes())
                except Exception:
                    pass

            return EngineMetricsSample(
                t_ms=now_ms,
                wall_iso=wall,
                engine_family=self.family.value,
                executors_active=executors_active,
                executors_total=executors_total,
                tasks_active=tasks_active,
                tasks_completed_total=tasks_completed,
                tasks_failed_total=tasks_failed,
                stages_completed_total=None,  # not surfaced by tracker directly
                stages_total=len(active_stage_ids),
                current_stage=current_stage_name,
                input_bytes_total=input_bytes or None,
                output_bytes_total=output_bytes or None,
                rows_written_total=(
                    int(getattr(self, "_rows_written_counter", 0)) or None
                ),
            )
        except Exception:
            # Any hiccup (JVM disconnected, tracker unavailable) →
            # return a bare sample. Never blow up the sampler.
            return EngineMetricsSample(
                t_ms=now_ms, wall_iso=wall, engine_family=self.family.value
            )


def spark_functions() -> EngineFunctions:
    """Return a session-less :class:`EngineFunctions` for the Spark family.

    The Spark ``functions`` DSL does not need a live session to build
    column expressions, so callers that already hold a ``SparkSession``
    (e.g. the ``apply_transforms`` mapper bridge op) can obtain the
    portable DSL without constructing a full :class:`PySparkEngine`.
    """
    return _SparkFunctions()


__all__ = ["PySparkEngine", "spark_functions"]