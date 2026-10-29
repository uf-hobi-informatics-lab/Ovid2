"""Per-invocation isolation for Ovid2 Docker resources.

Every invocation of the ETL pipeline creates its own :class:`OvidInstance`,
which generates a unique instance ID and derives instance-scoped names for
every shared Docker resource:

- Compose project name: ``ovid2-{id}``
- Master container:     ``ovid2-master-{id}``
- Worker containers:    ``ovid2-worker-{i}-{id}``
- Docker network:       ``ovid2-net-{id}``
- Compose file:         ``{WORK_DIR}/docker-compose-{id}.yml``
- Spark temp dir:       ``{WORK_DIR}/docker_data/spark_tmp-{id}/``
- Spark work dir:       ``{WORK_DIR}/docker_data/spark_work-{id}/``
- Spark logs dir:       ``{WORK_DIR}/docker_data/spark_logs-{id}/``
- Event log:            ``{WORK_DIR}/events-{id}.jsonl``
- nss-wrapper passwd:   ``{WORK_DIR}/nss-{id}/passwd``
- nss-wrapper group:    ``{WORK_DIR}/nss-{id}/group``

:func:`cleanup_stale_instances` is invoked once at CLI startup to reap
orphaned compose files, temp directories, and containers left behind by
processes that crashed before they could tear themselves down cleanly.

The nss-wrapper files exist so that the per-invocation containers can
``docker run --user $UID:$GID`` (running as the invoking host user) and
still have a valid ``getpwuid`` result — required for Java to resolve
``user.home`` to a real directory instead of ``"?"`` (which trips
``java.lang.IllegalArgumentException: basedir must be absolute:
?/.ivy2/local`` inside every Spark session). See
``docker/Dockerfile`` for the ``chmod 777`` of ``/docker_data/spark_tmp``
that makes an arbitrary uid's ``HOME`` writable, and
``docker/entrypoint.sh`` for the ``LD_PRELOAD=libnss_wrapper.so``
activation. Running as the host uid is required so the container can
read ``$XDG_CONFIG_HOME/ovid2/secrets.d/*.json`` (mode ``0o600``, owned
by the invoking user).

The per-invocation ``spark_work_dir`` and ``spark_logs_dir`` exist for
the same reason as the nss-wrapper overlay: the image's
``$SPARK_HOME/work`` and ``$SPARK_HOME/logs`` (``/opt/spark/…``) must be
writable by an arbitrary host uid, or
every worker JVM would fail ``IOException: Failed to create
directory $SPARK_HOME/work/app-<id>/<executor>/`` at executor
launch, ``MAX_EXECUTOR_RETRIES`` (10) failures later the standalone
master would mark the app FAILED, and the driver would crash mid-
``SparkSession.builder.getOrCreate()`` with ``Master removed our
application: FAILED`` + a cascade of listener NPEs. Bind-mounting a
host-writable dir at both container paths short-circuits the failure
regardless of what mode the baked-in dirs carry, which keeps the fix
working on images that predate the corresponding ``chmod`` in
``docker/Dockerfile``.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path

from ovid2.config.settings import WORK_DIR

logger = logging.getLogger(__name__)

DOCKER_DATA_DIR: Path = WORK_DIR / "docker_data"
DEFAULT_MAX_AGE_HOURS: int = 6

#: Regex used to accept an externally-supplied instance id. Enforcing the
#: canonical shape prevents a caller from smuggling shell metacharacters
#: into resource names that are later embedded in ``docker compose`` /
#: ``docker network`` invocations.
_INSTANCE_ID_PATTERN = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")

#: Docker network-name prefix owned by Ovid2's per-invocation isolation
#: (see :attr:`OvidInstance.network_name`). The network reaper is scoped to
#: this prefix so it never touches unrelated networks (e.g. the default
#: bridge, or the legacy ``ovid-*_spark_network`` from the legacy Ovid codebase).
_OVID2_NETWORK_PREFIX = "ovid2-net-"

#: ``docker network inspect`` timestamp format (RFC3339 with nanoseconds and
#: a numeric timezone offset, e.g. ``2026-08-19T01:48:27.123456789-04:00``).
#: Docker emits variable-width fractional seconds, so the reaper parses the
#: age defensively and treats any unparseable value as "old enough to reap"
#: only when the network is also empty.


def _generate_instance_id() -> str:
    """Return a fresh ``YYYYMMDD-HHMMSS-{6hex}`` instance ID."""
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _instance_id_from_env() -> str | None:
    """Return the ``OVID2_INSTANCE_ID`` env var when it matches the schema."""
    candidate = os.environ.get("OVID2_INSTANCE_ID", "").strip()
    if candidate and _INSTANCE_ID_PATTERN.match(candidate):
        return candidate
    if candidate:
        logger.warning(
            "Ignoring OVID2_INSTANCE_ID=%r — does not match YYYYMMDD-HHMMSS-6hex.",
            candidate,
        )
    return None


class OvidInstance:
    """Encapsulates all per-invocation isolation state.

    Attributes:
        instance_id:  Unique ``YYYYMMDD-HHMMSS-{6hex}`` identifier.
        project_name: Compose project name (``ovid2-{id}``).
        master_name:  Spark master container/hostname (``ovid2-master-{id}``).
        network_name: Docker network name (``ovid2-net-{id}``).
        compose_file: Path to the generated docker-compose file.
        spark_tmp_dir: Path to the instance-scoped Spark temp dir.
        spark_work_dir: Host path bind-mounted onto
            ``$SPARK_HOME/work`` in master + workers + driver.
            The Spark ``Worker`` process creates
            ``app-<id>/<executor>/`` subdirs here at executor-launch
            time; it must be writable by the invoking host uid.
        spark_logs_dir: Host path bind-mounted onto
            ``$SPARK_HOME/logs`` in master + workers + driver.
            The Spark ``Worker`` process opens its log file here at
            startup; same permission requirement as ``spark_work_dir``.
        event_log:    Path to the JSONL event log for this instance.
        nss_dir:      Directory holding the per-invocation nss-wrapper
            ``passwd`` and ``group`` files. See :meth:`ensure_nss_files`.

    Args:
        instance_id: When ``None``, look for a valid ``$OVID2_INSTANCE_ID``
            first and fall back to a freshly-generated id. This lets the
            web worker pre-assign an id before spawning ``ovid2 etl`` so
            it can tail the event log without a race.
    """

    def __init__(self, instance_id: str | None = None) -> None:
        chosen = instance_id or _instance_id_from_env() or _generate_instance_id()
        self.instance_id: str = chosen
        self.project_name: str = f"ovid2-{self.instance_id}"
        self.master_name: str = f"ovid2-master-{self.instance_id}"
        self.network_name: str = f"ovid2-net-{self.instance_id}"
        self.compose_file: Path = WORK_DIR / f"docker-compose-{self.instance_id}.yml"
        self.spark_tmp_dir: Path = DOCKER_DATA_DIR / f"spark_tmp-{self.instance_id}"
        self.spark_work_dir: Path = DOCKER_DATA_DIR / f"spark_work-{self.instance_id}"
        self.spark_logs_dir: Path = DOCKER_DATA_DIR / f"spark_logs-{self.instance_id}"
        self.event_log: Path = WORK_DIR / f"events-{self.instance_id}.jsonl"
        self.nss_dir: Path = WORK_DIR / f"nss-{self.instance_id}"

    # ------------------------------------------------------------------ names

    def worker_name(self, index: int) -> str:
        """Return the container name for the ``index``-th worker (1-based)."""
        return f"ovid2-worker-{index}-{self.instance_id}"

    def spark_master_url(self) -> str:
        """Return this instance's ``spark://…:7077`` master URL."""
        return f"spark://{self.master_name}:7077"

    # -------------------------------------------------------------- nss-wrapper

    @property
    def nss_passwd_file(self) -> Path:
        """Path to the per-invocation nss-wrapper ``passwd`` file."""
        return self.nss_dir / "passwd"

    @property
    def nss_group_file(self) -> Path:
        """Path to the per-invocation nss-wrapper ``group`` file."""
        return self.nss_dir / "group"

    def ensure_nss_files(
        self,
        uid: int | None = None,
        gid: int | None = None,
        *,
        home: str = "/docker_data/spark_tmp",
        username: str = "hostuser",
        groupname: str = "hostgrp",
    ) -> tuple[Path, Path]:
        """Write the nss-wrapper ``passwd`` and ``group`` overlay files.

        The runtime image ships ``libnss_wrapper.so`` (Debian
        ``libnss-wrapper``) at ``ovid2.core.docker.NSS_WRAPPER_LIB``.
        Bind-mounting these two files into
        the container at ``/etc/nss_wrapper/{passwd,group}`` and setting
        ``LD_PRELOAD=<NSS_WRAPPER_LIB>`` +
        ``NSS_WRAPPER_PASSWD`` + ``NSS_WRAPPER_GROUP`` in the container
        environment lets an arbitrary uid resolve via ``getpwuid`` — which
        Java calls under ``System.getProperty("user.home")`` at every
        ``SparkSession.builder.getOrCreate()``. Without it Java sees
        ``user.home = "?"`` and Spark crashes with
        ``java.lang.IllegalArgumentException: basedir must be absolute:
        ?/.ivy2/local``.

        Args:
            uid: Host uid to synthesise. Defaults to ``os.getuid()``.
            gid: Host gid to synthesise. Defaults to ``os.getgid()``.
            home: The synthesised ``pw_dir``. Defaults to the world-
                writable ``/docker_data/spark_tmp`` (chmod-777 in the
                Dockerfile).
            username: Login-name field for the synthesised entry.
            groupname: Group-name field for the synthesised entry.

        Returns:
            ``(passwd_path, group_path)``.
        """
        uid = os.getuid() if uid is None else uid
        gid = os.getgid() if gid is None else gid

        self.nss_dir.mkdir(parents=True, exist_ok=True)
        # World-readable so ``ls -l`` inside the container as the target
        # uid can enumerate; the files carry no secret material (just uid
        # + gid + home dir + login name).
        passwd_line = f"{username}:x:{uid}:{gid}:Ovid2 host user:{home}:/bin/false\n"
        group_line = f"{groupname}:x:{gid}:\n"
        self.nss_passwd_file.write_text(passwd_line, encoding="utf-8")
        self.nss_passwd_file.chmod(0o644)
        self.nss_group_file.write_text(group_line, encoding="utf-8")
        self.nss_group_file.chmod(0o644)
        return self.nss_passwd_file, self.nss_group_file

    # ---------------------------------------------------------------- lifecycle

    def ensure_directories(self) -> None:
        """Create the work dir, docker data dir, and Spark scratch dirs.

        Creates:

        - :data:`WORK_DIR` and :data:`DOCKER_DATA_DIR`.
        - :attr:`spark_tmp_dir` — legacy HOME/temp mount for the driver.
        - :attr:`spark_work_dir` and :attr:`spark_logs_dir` — mounted at
          ``$SPARK_HOME/{work,logs}`` in the containers so the
          Spark worker process (running as the invoking host uid) can
          create ``app-<id>/<executor>/`` subdirs and open its log
          file even when the baked-in dirs are not writable for the
          ``--user $UID:$GID`` override. These
          two mounts short-circuit the failure regardless of what
          mode the baked-in dirs carry.

        All three Spark scratch dirs are chmodded ``0o777`` so both
        the master (running as the host uid) and every worker JVM
        (same uid, sometimes forking executor JVMs that inherit a
        subset of the env) can write freely without stepping on each
        other's ownership.
        """
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        DOCKER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.spark_tmp_dir.mkdir(parents=True, exist_ok=True)
        self.spark_work_dir.mkdir(parents=True, exist_ok=True)
        self.spark_logs_dir.mkdir(parents=True, exist_ok=True)
        # ``0o777`` because the master and worker containers may all
        # run as the invoking host uid but the executor JVMs the
        # workers fork write files with a slightly different umask;
        # a permissive mode avoids any ownership races when the same
        # subdir is touched by multiple containers.
        for path in (self.spark_work_dir, self.spark_logs_dir):
            try:
                path.chmod(0o777)
            except OSError as exc:
                logger.debug("Could not chmod %s: %s", path, exc)

    def cleanup(self) -> None:
        """Remove this instance's compose file, Spark scratch dirs, and nss files.

        Safe to call multiple times; missing paths are silently ignored.
        The event log is left in place — the operator may still want to
        inspect it after the run.
        """
        try:
            self.compose_file.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("Could not remove compose file %s: %s", self.compose_file, exc)
        shutil.rmtree(self.spark_tmp_dir, ignore_errors=True)
        shutil.rmtree(self.spark_work_dir, ignore_errors=True)
        shutil.rmtree(self.spark_logs_dir, ignore_errors=True)
        shutil.rmtree(self.nss_dir, ignore_errors=True)

    def __repr__(self) -> str:
        return f"OvidInstance(id={self.instance_id!r})"


def _network_age_seconds(created: str, now: float) -> float | None:
    """Return the age in seconds of a docker ``.Created`` timestamp.

    Docker emits RFC3339 timestamps with variable-width fractional
    seconds and a numeric timezone offset. :meth:`datetime.fromisoformat`
    handles this on Python 3.11+. Returns ``None`` when the value cannot
    be parsed so the caller can decide how to treat an unknown age.

    Args:
        created: The ``{{.Created}}`` field from ``docker network inspect``.
        now: The current wall-clock time as a POSIX timestamp.

    Returns:
        Age in seconds, or ``None`` if ``created`` is empty/unparseable.
    """
    raw = (created or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return now - parsed.timestamp()


def _reap_orphaned_networks(max_age_hours: int, now: float | None = None) -> None:
    """Remove orphaned per-invocation Docker networks.

    A ``ovid2-net-<id>`` network is removed only when it has **zero
    attached containers** *and* is older than ``max_age_hours``. Networks
    that still carry containers (an in-flight run) or that are too young
    (a run mid-bring-up) are left alone. The reaper is scoped to the
    :data:`_OVID2_NETWORK_PREFIX` so it never touches the default bridge,
    unrelated project networks, or the legacy ``ovid-*_spark_network``.

    Docker's default address pools cap the number of concurrent networks
    (~31 with the out-of-the-box config); a run that crashes or is
    SIGKILLed before it can ``docker compose down`` leaks its network
    until this reaper reclaims it. Best-effort: any Docker error is
    logged at debug and skipped.

    Args:
        max_age_hours: Empty networks older than this are removed.
        now: Override for the current time (POSIX timestamp); used by tests.
    """
    now = time.time() if now is None else now
    max_age_seconds = max_age_hours * 3600

    try:
        listing = subprocess.run(
            [
                "docker",
                "network",
                "ls",
                "--filter",
                f"name=^{_OVID2_NETWORK_PREFIX}",
                "--format",
                "{{.Name}}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("Could not list docker networks for reaping: %s", exc)
        return
    if listing.returncode != 0:
        logger.debug(
            "docker network ls returned %d: %s",
            listing.returncode,
            listing.stderr.strip(),
        )
        return

    for name in listing.stdout.splitlines():
        name = name.strip()
        # Defensive: the ``name=^prefix`` filter is a substring/anchored
        # match in Docker, but re-check locally so a future filter change
        # can never widen the blast radius.
        if not name.startswith(_OVID2_NETWORK_PREFIX):
            continue
        try:
            inspected = subprocess.run(
                [
                    "docker",
                    "network",
                    "inspect",
                    name,
                    "--format",
                    "{{len .Containers}}|{{.Created}}",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.debug("Could not inspect network %s: %s", name, exc)
            continue
        if inspected.returncode != 0:
            continue

        count_str, _, created = inspected.stdout.strip().partition("|")
        try:
            container_count = int(count_str)
        except ValueError:
            # Unparseable inspect output — leave the network alone rather
            # than risk removing one that is in use.
            continue
        if container_count > 0:
            continue

        age = _network_age_seconds(created, now)
        # A parseable, recent age keeps the network; unparseable ages fall
        # through to removal (the network is already empty).
        if age is not None and age <= max_age_seconds:
            continue

        logger.warning("Reaping orphaned docker network: %s", name)
        try:
            subprocess.run(
                ["docker", "network", "rm", name],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.debug("Could not remove network %s: %s", name, exc)


def cleanup_stale_instances(max_age_hours: int = DEFAULT_MAX_AGE_HOURS) -> None:
    """Reap orphaned isolation resources left behind by crashed runs.

    Scans :data:`WORK_DIR` for compose files and temp directories older
    than ``max_age_hours``. For each stale compose file, attempts a
    ``docker compose down`` before deleting it. Finally, reaps orphaned
    per-invocation Docker networks (:func:`_reap_orphaned_networks`) so a
    run that crashed before it could ``docker compose down`` does not leak
    a subnet from Docker's finite default address pool.

    Args:
        max_age_hours: Files older than this many hours are removed.
    """
    now = time.time()
    max_age_seconds = max_age_hours * 3600

    if not WORK_DIR.is_dir():
        return

    for filepath in glob.glob(str(WORK_DIR / "docker-compose-*.yml")):
        try:
            age = now - os.path.getmtime(filepath)
        except OSError:
            continue
        if age <= max_age_seconds:
            continue

        logger.warning("Cleaning stale instance compose file: %s", filepath)
        try:
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    filepath,
                    "down",
                    "--remove-orphans",
                    "--timeout",
                    "10",
                ],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.debug("Could not tear down containers for %s: %s", filepath, exc)

        try:
            Path(filepath).unlink()
        except OSError as exc:
            logger.debug("Could not remove %s: %s", filepath, exc)

    if not DOCKER_DATA_DIR.is_dir():
        return

    for pattern in ("spark_tmp-*", "spark_work-*", "spark_logs-*"):
        for dirpath in glob.glob(str(DOCKER_DATA_DIR / pattern)):
            try:
                if not os.path.isdir(dirpath):
                    continue
                if now - os.path.getmtime(dirpath) <= max_age_seconds:
                    continue
            except OSError:
                continue

            logger.warning("Cleaning stale Spark scratch dir: %s", dirpath)
            shutil.rmtree(dirpath, ignore_errors=True)

    for dirpath in glob.glob(str(WORK_DIR / "nss-*")):
        try:
            if not os.path.isdir(dirpath):
                continue
            if now - os.path.getmtime(dirpath) <= max_age_seconds:
                continue
        except OSError:
            continue

        logger.warning("Cleaning stale nss-wrapper dir: %s", dirpath)
        shutil.rmtree(dirpath, ignore_errors=True)

    # Reclaim orphaned per-invocation networks last: a run that crashed or
    # was SIGKILLed before ``docker compose down`` leaks its subnet from
    # Docker's finite default address pool, eventually surfacing as
    # "all predefined address pools have been fully subnetted".
    _reap_orphaned_networks(max_age_hours, now=now)
