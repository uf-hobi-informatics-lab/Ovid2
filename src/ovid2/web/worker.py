"""Job worker — polls ``web_jobs`` and dispatches ``ovid2 etl`` subprocesses.

The worker is a small, single-process poll loop. It does not use a
process pool: each queued row is spawned via
:class:`subprocess.Popen`, tracked by its PID, and reaped when it
exits. Concurrency is bounded by
:data:`ovid2.web.settings.WEB_MAX_PARALLEL_JOBS`.

Design mirrors a sibling query-runner worker:

- One long-lived process, run under systemd (``ovid2-web-worker.service``).
- Crash recovery on start-up: any row still in ``running`` state whose
  PID is no longer alive is marked ``failed`` (or ``cancelled`` when
  the cancel flag was set).
- Cancellation is cooperative: the poller checks
  ``cancel_requested`` on every loop iteration and SIGTERMs the child.
- Standard error is captured to
  ``$XDG_STATE_HOME/ovid2/web_logs/<job_id>.log`` for the run detail
  page to display.

Nothing here imports pipeline code; the worker only spawns the CLI
subprocess and watches SQLite. That keeps the contract test
:func:`tests.contract.test_web_no_pipeline_imports` happy.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ovid2.config.settings import DATA_DIR, LEGACY_CREDENTIALS_DIR
from ovid2.run_kinds import get_kind
from ovid2.web import db as web_db
from ovid2.web.models import JobDetail, normalize_kind
from ovid2.web.services import jobs_svc, pipelines_svc, settings_svc, watch_svc
from ovid2.web.settings import (
    WEB_MAX_PARALLEL_JOBS,
    WEB_WORKER_POLL_SECONDS,
    WORKER_ENV_ALLOWLIST,
)

logger = logging.getLogger(__name__)


def _env_key_allowed(key: str, allowlist: tuple[str, ...]) -> bool:
    """Return True if ``key`` matches the allow-list.

    An entry ending in ``*`` is a prefix match; otherwise it is an exact
    match. See :data:`ovid2.web.settings.WORKER_ENV_ALLOWLIST`.
    """
    for pattern in allowlist:
        if pattern.endswith("*"):
            if key.startswith(pattern[:-1]):
                return True
        elif key == pattern:
            return True
    return False


def build_minimal_env(
    source_env: dict[str, str],
    allowlist: tuple[str, ...],
) -> dict[str, str]:
    """Return an allow-listed copy of ``source_env``.

    Only keys matching ``allowlist`` survive; everything else — including
    any ambient credentials exported into the worker's shell — is
    dropped. The executor receives its credentials exclusively from the
    scoped per-run secret mounts, never from inherited environment.
    """
    return {k: v for k, v in source_env.items() if _env_key_allowed(k, allowlist)}


@dataclass
class _Running:
    """In-memory record of a live subprocess."""

    job_id: str
    instance_id: str
    process: subprocess.Popen[bytes]
    log_handle: object = field(default=None)


class Worker:
    """Poll-and-dispatch loop.

    Args:
        max_parallel: Maximum simultaneous ``ovid2 etl`` subprocesses.
        poll_seconds: Sleep between poll iterations.
        ovid2_bin: Executable used to launch the ETL run. Defaults to
            ``sys.executable -m ovid2.cli`` so unit tests do not need
            the ``ovid2`` console script installed. Production
            deployments point this at ``/usr/local/bin/ovid2``.
    """

    def __init__(
        self,
        *,
        max_parallel: int = WEB_MAX_PARALLEL_JOBS,
        poll_seconds: float = WEB_WORKER_POLL_SECONDS,
        ovid2_bin: list[str] | None = None,
    ) -> None:
        self.max_parallel: int = max_parallel
        self.poll_seconds: float = poll_seconds
        self.ovid2_bin: list[str] = ovid2_bin or [sys.executable, "-m", "ovid2.cli"]
        self._running: dict[str, _Running] = {}
        self._stop = False

    # ------------------------------------------------------------------ loop

    def run_forever(self) -> None:
        """Block forever, polling the queue and reaping children.

        The loop exits cleanly on SIGTERM / SIGINT — every live child
        gets SIGTERM'd first, then the worker waits up to 30 seconds
        before returning.
        """
        web_db.ensure_schema()
        jobs_svc.resume_in_flight()
        signal.signal(signal.SIGTERM, self._on_shutdown_signal)
        signal.signal(signal.SIGINT, self._on_shutdown_signal)
        logger.info(
            "ovid2 web-worker started (max_parallel=%d, poll_seconds=%.1f)",
            self.max_parallel,
            self.poll_seconds,
        )
        try:
            while not self._stop:
                self.tick()
                time.sleep(self.poll_seconds)
        finally:
            self._drain()

    def tick(self) -> None:
        """One iteration of the poll loop.

        Exposed publicly so tests can drive the worker step by step.
        """
        self._reap_finished()
        self._handle_cancel_requests()
        self._dispatch_due_pipelines()
        self._dispatch_watch_pipelines()
        while len(self._running) < self.max_parallel:
            job = jobs_svc.claim_next_queued()
            if job is None:
                break
            self._spawn(job)

    def _dispatch_due_pipelines(self) -> None:
        """Fire every enabled pipeline whose ``next_fire_at`` is in the past.

        Reads the ``worker.route_dispatch_enabled`` flag from
        ``settings.json`` so operators can freeze all pipelines without
        editing files. Each fire submits a job through the ordinary
        :mod:`jobs_svc`; the worker will pick the row up on the next
        ``claim_next_queued`` iteration.
        """
        try:
            settings = settings_svc.get_settings()
        except Exception:
            logger.exception("settings_svc.get_settings failed; skipping pipeline dispatch.")
            return
        if not settings.worker.route_dispatch_enabled:
            return
        try:
            due = pipelines_svc.due_pipelines()
        except Exception:
            logger.exception("pipelines_svc.due_pipelines failed.")
            return
        for detail in due:
            try:
                response = pipelines_svc.run_now(detail.name, actor="worker")
                logger.info(
                    "Pipeline %s fired job %s (trigger=%s)",
                    detail.name, response.job_id, detail.trigger.type,
                )
            except pipelines_svc.PipelineError:
                logger.exception("Pipeline %s failed to fire.", detail.name)

    def _dispatch_watch_pipelines(self) -> None:
        """Scan every enabled ``watch`` pipeline and fire one job per new batch.

        For each enabled ``watch`` pipeline, poll its input storage account
        for top-level folders unseen since the stored cursor (via
        :func:`watch_svc.scan_for_new_batches`); submit one job per new
        batch with that folder name as the ``batch_id``, then advance the
        cursor so the same folder never fires twice.
        ``watch_min_wait_minutes`` debounces re-scans of the same input.
        Gated on the same ``worker.route_dispatch_enabled`` flag as
        clock-driven dispatch.
        """
        try:
            settings = settings_svc.get_settings()
        except Exception:
            logger.exception(
                "settings_svc.get_settings failed; skipping watch dispatch."
            )
            return
        if not settings.worker.route_dispatch_enabled:
            return
        try:
            summaries = pipelines_svc.list_pipelines()
        except Exception:
            logger.exception("pipelines_svc.list_pipelines failed.")
            return
        for summary in summaries:
            if summary.trigger_type != "watch" or not summary.enabled:
                continue
            detail = pipelines_svc.get_pipeline(summary.name)
            if detail is None:
                continue
            if self._watch_debounced(detail):
                continue
            try:
                new_batches = watch_svc.scan_for_new_batches(detail)
            except Exception:
                logger.exception("watch scan failed for pipeline %s.", detail.name)
                continue
            for batch_id in new_batches:
                try:
                    response = pipelines_svc.run_now(
                        detail.name, actor="worker", batch_id=batch_id
                    )
                    watch_svc.record_seen(
                        detail.name, detail.input_address, batch_id
                    )
                    logger.info(
                        "Watch pipeline %s fired job %s for new batch %s",
                        detail.name,
                        response.job_id,
                        batch_id,
                    )
                except pipelines_svc.PipelineError:
                    logger.exception(
                        "Watch pipeline %s failed to fire batch %s.",
                        detail.name,
                        batch_id,
                    )

    def _watch_debounced(self, detail: object) -> bool:
        """Return True when a watch pipeline was scanned too recently.

        Honours ``trigger.watch_min_wait_minutes``: a scan is skipped when
        fewer than that many minutes have elapsed since the cursor's
        ``last_checked_at``. ``None`` / ``0`` means scan every tick.
        """
        trigger = getattr(detail, "trigger", None)
        wait = getattr(trigger, "watch_min_wait_minutes", None)
        if not wait or wait <= 0:
            return False
        checked = watch_svc.last_checked_at(
            getattr(detail, "name", ""), getattr(detail, "input_address", "")
        )
        if not checked:
            return False
        try:
            last = datetime.fromisoformat(checked)
        except ValueError:
            return False
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed = datetime.now(timezone.utc) - last
        return elapsed < timedelta(minutes=int(wait))

    def _on_shutdown_signal(self, _signum: int, _frame: object) -> None:
        """Set the stop flag so :meth:`run_forever` exits after the next tick."""
        self._stop = True

    def _drain(self) -> None:
        """SIGTERM every child and wait for it to exit."""
        for record in list(self._running.values()):
            try:
                record.process.terminate()
            except ProcessLookupError:
                pass
        deadline = time.time() + 30
        while self._running and time.time() < deadline:
            self._reap_finished()
            time.sleep(0.2)
        # Anything still alive at this point gets killed.
        for record in list(self._running.values()):
            try:
                record.process.kill()
            except ProcessLookupError:
                pass
            self._finalize(record, returncode=-9, cancelled=True)

    # -------------------------------------------------------------- dispatch

    def _spawn(self, job: JobDetail) -> None:
        """Spawn ``ovid2 etl`` for ``job`` and record the child PID."""
        argv = self._build_argv(job)
        env = self._build_env(job)
        data_dir = Path(env.get("OVID2_DATA_DIR") or DATA_DIR)
        creds_dir = Path(env.get("OVID2_CREDENTIALS_DIR") or LEGACY_CREDENTIALS_DIR)
        data_exists = data_dir.is_dir()
        creds_exists = creds_dir.is_dir()
        # Surface the effective compose-mount sources at dispatch time. When
        # these fall back to the XDG defaults on a server host, the container
        # gets an empty ``/data`` mount and every read fails with
        # ``FileNotFoundError`` — the log line makes the misconfiguration
        # visible without needing to inspect the (short-lived) compose file.
        logger.info(
            "Dispatch mount sources: DATA_DIR=%s (exists=%s) "
            "CREDENTIALS_DIR=%s (exists=%s) job=%s instance=%s",
            data_dir,
            data_exists,
            creds_dir,
            creds_exists,
            job.job_id,
            job.instance_id,
        )
        log_path = jobs_svc.worker_log_path(job.job_id)
        log_handle = log_path.open("ab")
        header = (
            f"# ovid2 web-worker — job={job.job_id} "
            f"instance={job.instance_id} pipeline={job.pipeline} "
            f"batch={job.batch_id}\n"
            f"# argv={argv!r}\n"
            f"# DATA_DIR={data_dir} (exists={data_exists})"
            f" CREDENTIALS_DIR={creds_dir} (exists={creds_exists})\n"
        )
        log_handle.write(header.encode("utf-8"))
        log_handle.flush()
        try:
            process = subprocess.Popen(
                argv,
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            log_handle.close()
            logger.exception("Failed to spawn ovid2 etl for job %s", job.job_id)
            jobs_svc.mark_terminal(
                job.job_id, status=jobs_svc.STATUS_FAILED, error=f"spawn failed: {exc}"
            )
            return

        jobs_svc.mark_running_pid(job.job_id, process.pid)
        self._running[job.job_id] = _Running(
            job_id=job.job_id,
            instance_id=job.instance_id or "",
            process=process,
            log_handle=log_handle,
        )
        logger.info(
            "Spawned pid=%d for job=%s instance=%s pipeline=%s batch=%s",
            process.pid,
            job.job_id,
            job.instance_id,
            job.pipeline,
            job.batch_id,
        )

    def _build_argv(self, job: JobDetail) -> list[str]:
        """Return the ``ovid2 <subcommand> …`` argv for a job.

        The subcommand is selected by ``job.extra_args['kind']``:

        - ``etl``       → ``ovid2 etl``       (default; ``cdm`` is an
                          accepted alias).
        - ``analysis``  → ``ovid2 analysis``.
        - ``dq_checks`` → ``ovid2 dq_checks`` (EDC report-cycle checks;
                          reuses the health runner, so it carries
                          ``--packages`` / ``--parts`` / ``--checks``).
        - ``copy``      → ``ovid2 tools copy`` (post-C1; address-to-address
                          one-shot, bypasses formatter/mapper/fixer stages).
        - ``move``      → ``ovid2 tools move`` (post-M1; driver-native
                          successor to ``copy`` with ``--strict-types``
                          support).
        """
        kind = normalize_kind(job.extra_args.get("kind"))
        spec = get_kind(kind)
        builder = spec.argv_builder if spec else "etl"
        if builder == "copy":
            return self._build_argv_copy(job)
        if builder == "move":
            return self._build_argv_move(job)
        if builder == "health":
            return self._build_argv_health(job, kind)
        return self._build_argv_etl(job, kind)

    def _build_argv_health(self, job: JobDetail, kind: str = "health") -> list[str]:
        """Build the argv for a health-shaped job (``health`` / ``dq_checks``).

        Both kinds run the same check engine; the leading subcommand token
        is ``kind`` so ``dq_checks`` keeps its own identity (and its
        read-only ``/data`` mount). There is no formatter/mapper/fixer
        chain, so no ``--transformations``. Check selection travels via
        ``extra_args['parts']`` / ``extra_args['packages']`` /
        ``extra_args['checks']``; an optional prior refresh via
        ``extra_args['prev_input_address']``.
        """
        argv: list[str] = list(self.ovid2_bin) + [
            kind,
            "--pipeline",
            job.pipeline,
            "--batch_id",
            job.batch_id,
            "--input_address",
            job.input_address,
            "--output_address",
            job.output_address,
        ]
        parts = job.extra_args.get("parts")
        if parts:
            argv.append("--parts")
            argv.extend(str(p) for p in parts)
        packages = job.extra_args.get("packages")
        if packages:
            argv.append("--packages")
            argv.extend(str(p) for p in packages)
        checks = job.extra_args.get("checks")
        if checks:
            argv.append("--checks")
            argv.extend(str(c) for c in checks)
        prev = job.extra_args.get("prev_input_address")
        if prev:
            argv.extend(["--prev_input_address", str(prev)])
        engine = str(job.extra_args.get("engine") or "pyspark")
        argv.extend(["--engine", engine])
        workers = int(job.extra_args.get("workers") or 25)
        argv.extend(["--workers", str(workers)])
        cdm = job.extra_args.get("cdm")
        if cdm:
            argv.extend(["--cdm", str(cdm)])
        for cli_flag, key in (
            ("--sf_database", "sf_database"),
            ("--sf_account", "sf_account"),
            ("--sf_schema", "sf_schema"),
        ):
            value = job.extra_args.get(key)
            if value:
                argv.extend([cli_flag, str(value)])
        if job.extra_args.get("stderr_log"):
            argv.append("--stderr-log")
        return argv

    def _build_argv_etl(self, job: JobDetail, kind: str) -> list[str]:
        """Build the argv for the etl / analysis kinds.

        Historical ETL shape: ``--input_address``, ``--output_address``,
        ``--tables``, ``--transformations``, ``--engine``, ``--workers``,
        plus optional ``--sf_*`` / ``--cdm`` / ``--stderr-log``.
        (``dq_checks`` is no longer etl-shaped — it runs the health engine;
        see :meth:`_build_argv_health`.)
        """
        spec = get_kind(kind)
        subcommand = spec.cli_subcommand if spec else "etl"

        argv: list[str] = list(self.ovid2_bin) + [
            subcommand,
            "--pipeline",
            job.pipeline,
            "--batch_id",
            job.batch_id,
            "--input_address",
            job.input_address,
            "--output_address",
            job.output_address,
        ]
        if job.tables:
            argv.append("--tables")
            argv.extend(job.tables)
        if job.transformations:
            argv.append("--transformations")
            argv.extend(job.transformations)
        engine = str(job.extra_args.get("engine") or "pyspark")
        argv.extend(["--engine", engine])
        workers = int(job.extra_args.get("workers") or 25)
        argv.extend(["--workers", str(workers)])
        for cli_flag, key in (
            ("--sf_database", "sf_database"),
            ("--sf_account", "sf_account"),
            ("--sf_schema", "sf_schema"),
        ):
            value = job.extra_args.get(key)
            if value:
                argv.extend([cli_flag, str(value)])
        # `--cdm` is honoured only by ``ovid2 etl``; the other subcommands
        # ignore it. Emit it opportunistically when set to keep the write
        # path aware of which CDM's DDL to use.
        cdm = job.extra_args.get("cdm")
        if kind == "etl" and cdm:
            argv.extend(["--cdm", str(cdm)])
        if job.extra_args.get("stderr_log"):
            argv.append("--stderr-log")
        return argv

    def _build_argv_copy(self, job: JobDetail) -> list[str]:
        """Build the argv for a ``copy``-kind job.

        Emits ``ovid2 tools copy --from <input_address> --to
        <output_address> --pipeline … --batch_id … --tables …
        [--engine …]``. Deliberately drops every etl-only flag
        (``--transformations``, ``--workers``, ``--sf_*``, ``--cdm``,
        ``--stderr-log``) because ``ovid2 tools copy`` doesn't accept
        them; ``pipelines_svc._validate_kind`` also scrubs them at write
        time so a mis-authored pipeline can't reach this method with
        stale values.
        """
        if not job.tables:
            # Web-form validation should already have rejected this,
            # but defence-in-depth: ``tools copy`` requires
            # ``nargs='+'`` and would fail with a confusing argparse
            # error if we let an empty list through.
            raise ValueError(
                "copy jobs require an explicit `tables` list "
                "(job.tables is empty)."
            )
        engine = str(job.extra_args.get("engine") or "pyspark")
        argv: list[str] = list(self.ovid2_bin) + [
            "tools",
            "copy",
            "--from",
            job.input_address,
            "--to",
            job.output_address,
            "--pipeline",
            job.pipeline,
            "--batch_id",
            job.batch_id,
            "--tables",
            *job.tables,
            "--engine",
            engine,
        ]
        # ``extra_mounts`` — copy-kind-only ad-hoc bind mounts for the
        # per-invocation container. Persisted verbatim in
        # ``extra_args`` at submit time and forwarded to
        # ``ovid2 tools copy`` here. Validation (existence, container
        # path safety, mode) happens inside the CLI so the same rules
        # apply whether the caller is this worker, an interactive
        # shell, or MCP.
        for spec in job.extra_args.get("extra_mounts") or []:
            argv.extend(["--extra-mount", str(spec)])
        return argv

    def _build_argv_move(self, job: JobDetail) -> list[str]:
        """Build the argv for a ``move``-kind job.

        Emits ``ovid2 tools move --from <input_address> --to
        <output_address> --pipeline … --batch_id … --tables …
        [--engine …] [--strict-types] [--extra-mount …]``. Mirrors
        :meth:`_build_argv_copy` — same subset of etl-only flags
        dropped (``--transformations``, ``--workers``, ``--sf_*``,
        ``--cdm``, ``--stderr-log``) because ``ovid2 tools move``
        doesn't accept them — plus one addition: ``--strict-types``
        is emitted when ``extra_args["strict_types"]`` is truthy.
        """
        if not job.tables:
            # Web-form validation should already have rejected this,
            # but defence-in-depth: ``tools move`` requires
            # ``nargs='+'`` and would fail with a confusing argparse
            # error if we let an empty list through.
            raise ValueError(
                "move jobs require an explicit `tables` list "
                "(job.tables is empty)."
            )
        engine = str(job.extra_args.get("engine") or "pyspark")
        argv: list[str] = list(self.ovid2_bin) + [
            "tools",
            "move",
            "--from",
            job.input_address,
            "--to",
            job.output_address,
            "--pipeline",
            job.pipeline,
            "--batch_id",
            job.batch_id,
            "--tables",
            *job.tables,
            "--engine",
            engine,
        ]
        if job.extra_args.get("strict_types"):
            argv.append("--strict-types")
        # ``extra_mounts`` — same rules as the copy path: validation
        # happens inside the CLI so the worker forwards raw strings.
        for spec in job.extra_args.get("extra_mounts") or []:
            argv.extend(["--extra-mount", str(spec)])
        return argv

    def _build_env(self, job: JobDetail) -> dict[str, str]:
        """Return a least-privilege environment for the executor.

        The child inherits only the allow-listed subset of the worker's
        environment (see :data:`WORKER_ENV_ALLOWLIST`) plus
        ``OVID2_INSTANCE_ID``. Ambient credentials in the worker's own
        environment are never propagated — the executor gets credentials
        exclusively from its scoped per-run secret mounts. See
        the adr-harness-api-executor-trust-model design note.
        """
        env = build_minimal_env(dict(os.environ), WORKER_ENV_ALLOWLIST)
        if job.instance_id:
            env["OVID2_INSTANCE_ID"] = job.instance_id
        # The child writes to a log *file*, so Python would block-buffer
        # its own prints ("Starting Spark cluster…") while docker writes
        # straight through — the worker log then showed host-side steps
        # *after* "Health complete". Unbuffered keeps the log in order.
        env["PYTHONUNBUFFERED"] = "1"
        return env

    # ---------------------------------------------------------------- reaping

    def _reap_finished(self) -> None:
        """Look for children that have exited and finalize their rows."""
        for job_id, record in list(self._running.items()):
            returncode = record.process.poll()
            if returncode is None:
                continue
            self._finalize(record, returncode=returncode, cancelled=False)
            del self._running[job_id]

    def _handle_cancel_requests(self) -> None:
        """SIGTERM children whose row has ``cancel_requested = 1``."""
        if not self._running:
            return
        with web_db.connect() as conn:
            rows = conn.execute(
                "SELECT job_id FROM web_jobs "
                "WHERE cancel_requested = 1 AND status = ?",
                (jobs_svc.STATUS_RUNNING,),
            ).fetchall()
        for row in rows:
            record = self._running.get(str(row["job_id"]))
            if record is None:
                continue
            # 1) SIGTERM the host-side ``ovid2`` process. Its signal
            #    handler stops the driver container (which delivers
            #    SIGTERM to the in-container ``ovid2 etl`` so it can
            #    cancel the in-flight Snowflake query) and tears down
            #    the cluster.
            try:
                record.process.terminate()
                logger.info(
                    "SIGTERM'd pid=%d for job=%s", record.process.pid, record.job_id
                )
            except ProcessLookupError:
                pass
            # 2) Belt-and-suspenders: directly stop the driver container
            #    by name too, in case the host process is wedged or was
            #    SIGKILLed before its handler ran. ``docker stop`` on a
            #    gone/never-started container is a harmless no-op. Runs
            #    in a background thread so a slow ``docker stop`` grace
            #    period doesn't block the worker poll loop.
            self._stop_driver_container_async(record.instance_id)
            # 3) Belt-and-suspenders for the *cluster*: tear the compose
            #    project down by name too. The host process's SIGTERM
            #    handler normally does this (``stop_spark_cluster``), but
            #    if that process is wedged / SIGKILLed the 25-worker
            #    cluster + its network would leak (observed: a cancelled
            #    run left 26 containers up and its subnet allocated,
            #    contributing to address-pool exhaustion). ``compose
            #    down`` on an already-gone project is a harmless no-op.
            self._stop_cluster_async(record.instance_id)

    def _stop_driver_container_async(self, instance_id: str | None) -> None:
        """Stop the named driver container off the poll thread.

        The driver container is named ``ovid2-driver-<instance_id>`` by
        :func:`ovid2.core.docker.driver_container_name`. We reproduce
        that name here (rather than importing the pipeline helper) so the
        web tier stays free of ``ovid2.core`` imports
        (``test_web_no_pipeline_imports``). ``docker stop`` delivers
        SIGTERM to the container's PID 1 (the ``ovid2 etl`` process),
        which runs its in-container cancel handler (Snowpark
        ``SYSTEM$CANCEL_ALL_QUERIES``) before exiting. A gone /
        never-started container is a harmless no-op.
        """
        if not instance_id:
            return
        safe = "".join(
            c if (c.isalnum() or c in "._-") else "-" for c in instance_id
        )
        name = f"ovid2-driver-{safe}"

        def _run() -> None:
            try:
                subprocess.run(
                    ["docker", "stop", "--timeout", "35", name],
                    capture_output=True,
                    check=False,
                    timeout=60,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                logger.debug(
                    "driver container stop for %s failed: %s", name, exc
                )

        threading.Thread(
            target=_run,
            name=f"ovid2-cancel-{instance_id}",
            daemon=True,
        ).start()

    def _stop_cluster_async(self, instance_id: str | None) -> None:
        """Tear down the compose project for a cancelled run, off-thread.

        The per-invocation compose project is named ``ovid2-<instance_id>``
        by :class:`ovid2.core.isolation.OvidInstance`. We reproduce that
        name here (rather than importing the pipeline helper) so the web
        tier stays free of ``ovid2.core`` imports
        (``test_web_no_pipeline_imports``). ``docker compose -p
        ovid2-<id> down --remove-orphans`` stops every master/worker
        container and removes the project's network; it is a harmless
        no-op if the host process's own SIGTERM handler already tore the
        cluster down. Runs in a background thread so a slow shutdown
        grace period doesn't block the worker poll loop.

        This is the belt-and-suspenders guarantee that a cancel never
        leaks the cluster (and its subnet) when the host executor process
        is wedged or was SIGKILLed before its ``stop_spark_cluster``
        handler could run.
        """
        if not instance_id:
            return
        safe = "".join(
            c if (c.isalnum() or c in "._-") else "-" for c in instance_id
        )
        project = f"ovid2-{safe}"

        def _run() -> None:
            try:
                subprocess.run(
                    [
                        "docker",
                        "compose",
                        "-p",
                        project,
                        "down",
                        "--remove-orphans",
                        "--timeout",
                        "35",
                    ],
                    capture_output=True,
                    check=False,
                    timeout=90,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                logger.debug(
                    "cluster teardown for %s failed: %s", project, exc
                )

        threading.Thread(
            target=_run,
            name=f"ovid2-cancel-cluster-{instance_id}",
            daemon=True,
        ).start()

    def _finalize(
        self,
        record: _Running,
        *,
        returncode: int,
        cancelled: bool,
    ) -> None:
        """Update the DB and close the log handle for a finished child."""
        try:
            record.log_handle.close()  # type: ignore[union-attr]
        except Exception:  # pragma: no cover — defensive
            pass
        # Refresh cancel_requested from the DB so a race between the
        # SIGTERM and the exit is still attributed correctly.
        detail = jobs_svc.get_job(record.job_id)
        was_cancel = cancelled or (detail is not None and detail.cancel_requested)
        if was_cancel and returncode != 0:
            jobs_svc.mark_terminal(
                record.job_id, status=jobs_svc.STATUS_CANCELLED, error=None
            )
        elif returncode == 0:
            jobs_svc.mark_terminal(record.job_id, status=jobs_svc.STATUS_DONE)
        else:
            # Try to tail the worker log for a human-readable last-error
            # line. The plan-time aborts in ``ovid2 tools copy`` /
            # ``ovid2 tools move`` (engine-compat, table-spec grammar,
            # extra-mount validation) all print a single ``error: <why>``
            # line and exit rc=2 — so an operator seeing "job: failed"
            # on the run detail page gets the actual reason inline
            # instead of having to open the worker log.
            log_path = jobs_svc.worker_log_path(record.job_id)
            summary = _extract_error_from_log(log_path)
            fallback = f"ovid2 etl exited with status {returncode}"
            error_message = summary or fallback
            jobs_svc.mark_terminal(
                record.job_id,
                status=jobs_svc.STATUS_FAILED,
                error=error_message,
            )
        logger.info(
            "Finalized job=%s returncode=%d cancelled=%s",
            record.job_id,
            returncode,
            was_cancel,
        )


# ---------------------------------------------------------------------------
# Log-tailing helper
# ---------------------------------------------------------------------------


#: Max bytes to read from the tail of a worker log when looking for an
#: error summary. A full ETL run can produce megabytes of Spark output;
#: bounding the read keeps the SQLite update fast and the memory
#: footprint tiny.
_LOG_TAIL_BYTES = 64 * 1024

#: Max length of the summary line we stash in ``web_jobs.error``. Longer
#: lines are truncated with an ellipsis so the run-detail banner stays
#: readable and the SQLite row stays small.
_LOG_SUMMARY_MAX = 800

#: Substring prefixes that identify docker-compose orchestration noise
#: (``Container … Created`` / ``Container … Started`` /
#: ``Network … Creating``, etc.). These lines are the loudest thing in
#: the log tail for a fast plan-time failure — the actual error is
#: printed *above* the compose cleanup — so we skip past them.
_COMPOSE_NOISE_MARKERS = (
    " Container ovid2-",
    " Network ovid2-",
    "Container ovid2-",
    "Network ovid2-",
)

#: Regex for a Python exception line — the terminal line of a
#: ``Traceback (most recent call last):`` block. When the worker log
#: contains a raw Python traceback (as opposed to a
#: ``print("error: ...", file=sys.stderr)`` plan-time abort), we'd
#: rather surface e.g. ``ModuleNotFoundError: No module named
#: 'snowflake.snowpark'`` than the generic ``exited with status 1``.
#: The pattern intentionally accepts qualified exception paths
#: (``pyspark.sql.utils.AnalysisException: ...``) and one-name
#: bare exceptions (``ValueError: ...``). Java-style exceptions
#: (``java.lang.IllegalArgumentException: ...``) are also matched —
#: PySpark surfaces them frequently when a JVM check fails before
#: the Python side gets to raise.
_EXCEPTION_LINE_RE = re.compile(
    r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
    r"(?:Error|Exception|Warning|NotImplementedError|Interrupt|Failure)"
    r"(?:\s*:\s*.*)?$",
)


def _extract_error_from_log(log_path: Path) -> str | None:
    """Return the last operator-facing error line from a worker log.

    The worker captures stdout + stderr merged into
    ``$XDG_STATE_HOME/ovid2/web_logs/<job_id>.log``. This helper reads
    the last :data:`_LOG_TAIL_BYTES` bytes, walks the lines from the
    bottom up, and returns the first line that:

    1. Is not empty (whitespace-only lines skipped).
    2. Does not start with ``#`` (the header lines the worker itself
       writes are noise from the operator's point of view).
    3. Is not docker-compose progress noise
       (``Container ovid2-… Created`` / ``Network …``, matched via
       :data:`_COMPOSE_NOISE_MARKERS`).
    4. Is not a bare ``[ovid2] Stopping Spark cluster …`` cleanup
       message.

    Args:
        log_path: The worker log path (as returned by
            :func:`ovid2.web.services.jobs_svc.worker_log_path`).

    Returns:
        The trimmed error line, capped at :data:`_LOG_SUMMARY_MAX`
        characters (with a trailing ``…`` when truncated), or ``None``
        when the file is missing, unreadable, or contains no
        operator-facing content. ``None`` signals the caller to fall
        back to the generic ``ovid2 etl exited with status N`` string.
    """
    try:
        size = log_path.stat().st_size
    except (OSError, FileNotFoundError):
        return None
    if size == 0:
        return None
    try:
        with log_path.open("rb") as fh:
            if size > _LOG_TAIL_BYTES:
                fh.seek(size - _LOG_TAIL_BYTES)
                # Discard the first (likely partial) line so we don't
                # emit a truncated middle-of-a-line as the summary.
                fh.readline()
            data = fh.read()
    except OSError:
        return None

    # decode leniently — the child process may have emitted invalid
    # UTF-8 (Spark stderr occasionally does), and we'd rather return
    # a slightly-mangled real message than nothing.
    text = data.decode("utf-8", errors="replace")

    lines = text.splitlines()

    # First pass: hunt for a Python exception line. On a container-
    # side crash (e.g. ``ModuleNotFoundError: No module named
    # 'snowflake.snowpark'``) the traceback is followed by
    # ``[ovid2] Stopping Spark cluster ...`` and the ``docker
    # compose down`` progress lines — the tail-walk below would skip
    # every one of those and *should* land on the exception line, but
    # only when it wasn't itself buried under further compose noise.
    # Preferring an exception line unconditionally (when one is
    # present) closes the incident where a partial log tail hid the
    # traceback under compose ``Removing``/``Removed`` progress.
    #
    # We take the *last* exception line in the tail so a nested
    # exception's final ``KeyError: ...`` still surfaces above the
    # outer ``ExceptionGroup`` framing.
    for raw_line in reversed(lines):
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if _EXCEPTION_LINE_RE.match(stripped):
            return _cap(stripped)

    # Second pass: bottom-up walk, skipping known noise.
    for raw_line in reversed(lines):
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if any(marker in line for marker in _COMPOSE_NOISE_MARKERS):
            continue
        # ``[ovid2] Stopping Spark cluster …`` and its friends are the
        # last-lines of a normal cluster teardown — they arrive after
        # the real error, so we skip them to find the actual message.
        if stripped.startswith("[ovid2] Stopping") or \
           stripped.startswith("[ovid2] Spark cluster ready") or \
           stripped.startswith("[ovid2] Starting Spark cluster") or \
           stripped.startswith("[ovid2] Cluster not fully ready") or \
           stripped.startswith("[ovid2] Shutdown reported errors") or \
           stripped.startswith("[ovid2] Received "):
            continue
        # ``docker compose`` (Go binary) emits a top-of-log warning
        # like ``time="…" level=warning msg="… version is obsolete
        # …"`` that has nothing to do with any failure. Skip it so
        # it can't be the last-line for an otherwise-empty error.
        if stripped.startswith("time=") and "level=" in stripped:
            continue
        return _cap(stripped)
    return None


def _cap(stripped: str) -> str:
    """Truncate ``stripped`` to :data:`_LOG_SUMMARY_MAX` with an ellipsis."""
    if len(stripped) > _LOG_SUMMARY_MAX:
        return stripped[: _LOG_SUMMARY_MAX - 1] + "\u2026"
    return stripped


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``web-worker`` subcommand."""
    parser = subparsers.add_parser(
        "web-worker",
        help="Poll the web-tier job queue and dispatch ovid2 etl subprocesses.",
        description=(
            "Long-running daemon that consumes rows from web_jobs and "
            "spawns ovid2 etl subprocesses. Deploy under systemd via "
            "ovid2-web-worker.service."
        ),
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=WEB_MAX_PARALLEL_JOBS,
        help=f"Maximum concurrent subprocesses (default: {WEB_MAX_PARALLEL_JOBS}).",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=WEB_WORKER_POLL_SECONDS,
        help=f"Poll interval in seconds (default: {WEB_WORKER_POLL_SECONDS}).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one tick and exit (useful for smoke testing).",
    )
    parser.set_defaults(_command="web-worker", _entry=main)


def main(args: argparse.Namespace) -> int:
    """Entry point for the ``ovid2 web-worker`` subcommand."""
    worker = Worker(max_parallel=args.max_parallel, poll_seconds=args.poll_seconds)
    if args.once:
        web_db.ensure_schema()
        jobs_svc.resume_in_flight()
        worker.tick()
        return 0
    worker.run_forever()
    return 0
