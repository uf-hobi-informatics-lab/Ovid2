"""Docker and Spark-cluster management with per-invocation isolation.

Handles container lifecycle, ``spark-submit`` command construction, and
cluster start/stop for local and cluster Spark modes. Every function
accepts an :class:`~ovid2.core.isolation.OvidInstance` so concurrent runs
on the same Docker host never collide on container/network/project names.

Signal handlers are installed inside :func:`submit_with_cluster` so that
an interrupted process still tears down its containers before exiting.

.. note::

    This module is host-side code — it runs *outside* Docker. It is
    allowed to use ``print`` and shell out to ``docker`` / ``docker compose``
    directly (per ``AGENTS.md`` §Style rules).
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import FrameType
from typing import Any

from ovid2.config.settings import (
    DATA_DIR,
    DOCKER_IMAGE,
    LEGACY_CREDENTIALS_DIR,
    SPARK_CONFIG,
    SPARK_CORES,
    SPARK_LOCAL_DIR_IN_CONTAINER,
    SPARK_MODE,
    WORK_DIR,
)
from ovid2.core.isolation import OvidInstance

logger = logging.getLogger(__name__)


#: Absolute path of the ``libnss_wrapper.so`` shim inside the runtime
#: image (Debian ``libnss-wrapper``, symlinked to a multiarch-neutral
#: location by ``docker/Dockerfile``). See :func:`nss_wrapper_env` below
#: and :meth:`OvidInstance.ensure_nss_files` for the design rationale.
NSS_WRAPPER_LIB: str = "/usr/local/lib/libnss_wrapper.so"

#: ``$SPARK_HOME`` inside the runtime image — a symlink to the pip
#: ``pyspark`` package (``docker/Dockerfile`` pins ``ENV SPARK_HOME``).
#: Every container-side Spark path derives from it.
SPARK_HOME_IN_CONTAINER: str = "/opt/spark"

#: ``spark-class`` launcher used to run the standalone Master / Worker
#: in the foreground (see :func:`ovid2.core.compose.build_compose_dict`).
SPARK_CLASS_IN_CONTAINER: str = f"{SPARK_HOME_IN_CONTAINER}/bin/spark-class"

#: Per-executor scratch dir a standalone Worker writes app dirs into.
SPARK_WORK_DIR_IN_CONTAINER: str = f"{SPARK_HOME_IN_CONTAINER}/work"

#: Spark daemon log dir.
SPARK_LOGS_DIR_IN_CONTAINER: str = f"{SPARK_HOME_IN_CONTAINER}/logs"

#: In-container path for the synthetic ``passwd`` overlay.
NSS_PASSWD_IN_CONTAINER: str = "/etc/nss_wrapper/passwd"  # noqa: S105 — a path, not a secret

#: In-container path for the synthetic ``group`` overlay.
NSS_GROUP_IN_CONTAINER: str = "/etc/nss_wrapper/group"


def _host_user() -> tuple[int, int]:
    """Return ``(uid, gid)`` of the invoking host user.

    Split out so tests can monkeypatch the pair without touching the
    process-wide values.
    """
    return os.getuid(), os.getgid()


def nss_wrapper_env(instance: OvidInstance) -> tuple[list[str], list[str]]:
    """Return ``(env_flags, mount_flags)`` that make ``--user`` work in the image.

    The container image runs as ``USER 1001`` by default. To read
    ``secrets.d/*.json`` (mode ``0o600``, owned by the invoking host
    user, per AGENTS.md §7 and ``test_web_secret_permissions.py``) we
    have to override ``--user`` to the host uid — but that leaves
    ``getpwuid()`` returning ``NULL`` inside the container, which
    breaks Java's ``System.getProperty("user.home")`` and prevents any
    ``SparkSession`` from launching. ``libnss_wrapper.so`` (already
    installed in the image at :data:`NSS_WRAPPER_LIB`) intercepts the
    ``getpwuid`` / ``getgrgid`` calls and consults the passwd/group
    overlay files we bind-mount instead.

    Callers must:

    - Add ``env_flags`` to the ``docker run`` argv (or to the compose
      service's ``environment:``).
    - Add ``mount_flags`` to the ``docker run`` argv (or to the compose
      service's ``volumes:``).
    - Also emit ``--user <uid>:<gid>`` (or ``user:`` on compose).

    Args:
        instance: The per-invocation context. Its
            :meth:`~OvidInstance.ensure_nss_files` is called here so the
            two overlay files exist on the host before ``docker run``
            evaluates its bind mounts (docker refuses to start when a
            bind source is missing).

    Returns:
        A pair of argv-shaped lists suitable for splatting into a
        ``docker run`` command.
    """
    passwd_path, group_path = instance.ensure_nss_files()
    env_flags = [
        "-e",
        f"LD_PRELOAD={NSS_WRAPPER_LIB}",
        "-e",
        f"NSS_WRAPPER_PASSWD={NSS_PASSWD_IN_CONTAINER}",
        "-e",
        f"NSS_WRAPPER_GROUP={NSS_GROUP_IN_CONTAINER}",
    ]
    mount_flags = [
        "-v",
        f"{passwd_path}:{NSS_PASSWD_IN_CONTAINER}:ro",
        "-v",
        f"{group_path}:{NSS_GROUP_IN_CONTAINER}:ro",
    ]
    return env_flags, mount_flags


def is_inside_docker() -> bool:
    """Return ``True`` if the current process is running inside a container."""
    return Path("/.dockerenv").exists()


def _parse_memory_gb(mem_str: str) -> float:
    """Parse a Spark memory string (``'32g'``, ``'4096m'``) into gigabytes."""
    normalized = mem_str.lower()
    if normalized.endswith("g"):
        return float(normalized[:-1])
    if normalized.endswith("m"):
        return float(normalized[:-1]) / 1024
    return float(normalized)


def driver_container_name(instance_id: str) -> str:
    """Return the deterministic driver-container name for an instance.

    The per-invocation driver container (the ``docker run`` that hosts
    ``ovid2 etl`` / ``tools copy`` / ``tools move``) is named
    ``ovid2-driver-<instance_id>`` so a cancel path can address it by
    name — ``docker stop`` delivers SIGTERM to the container's PID 1
    (the entrypoint → ``ovid2``), which triggers the in-container
    signal handler (e.g. Snowpark's ``SYSTEM$CANCEL_ALL_QUERIES``) and
    then reaps the container. Without a stable name the host has no
    handle on the container after ``subprocess.run`` returns / is
    killed, and a cancelled Snowpark run leaks a live Snowflake query.

    The instance id (``YYYYMMDD-HHMMSS-<6hex>``) is already a valid
    docker name fragment; we still sanitise defensively so any future
    id shape can't produce an invalid ``--name``.
    """
    safe = "".join(c if (c.isalnum() or c in "._-") else "-" for c in instance_id)
    return f"ovid2-driver-{safe}"


#: argparse dests that name an *address* the run dereferences.
SCOPED_ADDRESS_ARGS: tuple[str, ...] = (
    "input_address",
    "output_address",
    "from_address",
    "to_address",
    "prev_input_address",
)

#: argparse dests that name a *connection* the run dereferences.
SCOPED_CONNECTION_ARGS: tuple[str, ...] = ("input_connection", "output_connection", "connection")

#: Container path the granted secret files are bound under; exported to
#: the container as ``OVID2_SECRETS_DIR``.
SCOPED_SECRETS_IN_CONTAINER: str = "/ovid2-scoped-secrets"


def _addresses_for_args(args: Any) -> list[str]:
    """Return the address names a run references, for secret scoping.

    Covers every dest in :data:`SCOPED_ADDRESS_ARGS` (ETL family, copy /
    move, and the health ``--prev_input_address`` refresh). Missing
    attributes are skipped; order is preserved, duplicates collapsed.
    """
    names: list[str] = []
    for attr in SCOPED_ADDRESS_ARGS:
        value = getattr(args, attr, None)
        if value and value not in names:
            names.append(str(value))
    return names


def secret_scope_for_args(args: Any) -> Any:
    """Return the :class:`~ovid2.config.secret_refs.SecretScope` of a run.

    Addresses, connections (health's ``--input_connection`` /
    ``--output_connection``), the engine, and the pipeline (salts + the
    XOR seed). Pinned by
    ``tests/contract/test_executor_subprocess_env_scoped.py``.
    """
    from ovid2.config.secret_refs import SecretScope
    from ovid2.config.settings import SEED_FILE_NAME

    connections = tuple(
        str(getattr(args, a)) for a in SCOPED_CONNECTION_ARGS if getattr(args, a, None)
    )
    engine = getattr(args, "engine", None)
    pipeline = getattr(args, "pipeline", None)
    return SecretScope(
        connections=connections,
        addresses=tuple(_addresses_for_args(args)),
        engines=(str(engine),) if engine else (),
        pipelines=(str(pipeline),) if pipeline else (),
        extra_refs=(SEED_FILE_NAME,) if pipeline else (),
    )


def _granted_secret_files(args: Any) -> list[Path]:
    """Return host paths of the secret files this run was granted.

    When the host process already runs inside a scoped executor env
    (``OVID2_SECRETS_DIR`` set by the web worker), that directory *is*
    the grant. Otherwise (direct CLI invocation) the scope is derived
    from ``args`` and resolved against the normal ``secrets.d`` search
    path — names only, values are never read here.
    """
    from ovid2.config.executor_env import secret_source_dirs
    from ovid2.config.secret_refs import resolve_scope

    scoped = os.environ.get("OVID2_SECRETS_DIR")
    if scoped and Path(scoped).is_dir():
        return sorted(p for p in Path(scoped).iterdir() if p.is_file())
    try:
        wanted = resolve_scope(secret_scope_for_args(args))
    except Exception:
        logger.warning("Could not compute scoped secret refs; granting none.")
        return []
    files: list[Path] = []
    for name in sorted(wanted):
        src = next(
            (d / name for d in secret_source_dirs() if (d / name).is_file()),
            None,
        )
        if src is None:
            logger.debug("Secret %r referenced by this run has no file; skipping.", name)
            continue
        files.append(src)
    return files


def _scoped_secret_mounts(args: Any, host_xdg_config: Path) -> list[str]:
    """Return docker flags scoping the container's secret exposure.

    - Every granted secret file is bind-mounted read-only under
      :data:`SCOPED_SECRETS_IN_CONTAINER`, exported as
      ``OVID2_SECRETS_DIR`` (consulted first by ``load_secret``).
    - Under ``OVID2_SECRETS_SCOPE_MODE=enforce`` the mounted config
      tree's real ``secrets.d`` (``/ovid2-user-config/ovid2/secrets.d``)
      is shadowed by an empty tmpfs, so nothing outside the grant is
      reachable at all. Under the default ``warn`` it stays visible and
      out-of-scope reads log a warning (roll-out mode).

    Historical note: before 2026-11-04 this shadowed
    ``/ovid2-user-config/secrets.d`` — one level too shallow (the tree is
    ``$XDG_CONFIG_HOME/ovid2/secrets.d``) — so no secret was ever hidden.
    """
    from ovid2.config.secrets import SCOPE_MODE_ENV, scope_enforced

    flags: list[str] = []
    for src in _granted_secret_files(args):
        flags.extend(["-v", f"{src}:{SCOPED_SECRETS_IN_CONTAINER}/{src.name}:ro"])
    flags.extend(["-e", f"OVID2_SECRETS_DIR={SCOPED_SECRETS_IN_CONTAINER}"])
    enforce = scope_enforced()
    flags.extend(["-e", f"{SCOPE_MODE_ENV}={'enforce' if enforce else 'warn'}"])
    if enforce and (host_xdg_config / "ovid2" / "secrets.d").is_dir():
        flags.extend(["--tmpfs", "/ovid2-user-config/ovid2/secrets.d:ro,size=64k"])
    return flags


def effective_spark_config(*, cluster_mode: bool) -> dict[str, str]:
    """Return :data:`SPARK_CONFIG` adjusted for the run mode and host.

    Cluster mode keeps the configured driver heap. Local mode runs every
    task inside the driver JVM, so its heap is sized from host RAM
    (:func:`ovid2.config.spark_sizing.local_driver_memory_gb`) unless the
    operator pinned ``spark.driver.memory`` via
    ``$OVID2_SPARK_LOCAL_DRIVER_MEMORY_GB``. Executor settings are inert
    in local mode and left untouched.
    """
    from ovid2.config import spark_sizing

    config = dict(SPARK_CONFIG)
    if not cluster_mode:
        config["spark.driver.memory"] = f"{spark_sizing.local_driver_memory_gb()}g"
    return config


def duckdb_memory_limit(container_memory_gb: int) -> str:
    """Return the DuckDB ``memory_limit`` for a container capped at ``container_memory_gb``.

    An operator-set ``$OVID2_DUCKDB_MEMORY_LIMIT`` on the host wins;
    otherwise 85% of the container cap (at least 1 GB).
    """
    override = os.environ.get("OVID2_DUCKDB_MEMORY_LIMIT", "").strip()
    if override:
        return override
    return f"{max(1, int(container_memory_gb * 0.85))}GB"


def _is_duckdb_engine(args: Any) -> bool:
    """Return whether ``args.engine`` resolves to the DuckDB family."""
    from ovid2.config.engines import engine_family, resolve_engine_type

    engine_type = resolve_engine_type(getattr(args, "engine", None))
    return engine_type is not None and engine_family(engine_type) == "duckdb"


def build_docker_command(args: Any, instance: OvidInstance) -> list[str]:
    """Build the ``docker run`` command for an Ovid2 subcommand.

    The container ENTRYPOINT is ``ovid2 …``, so the argv suffix is
    whatever :func:`_build_ovid2_argv` returns for ``args.command``.
    Supported commands: ``etl`` / ``analysis`` / ``dq_checks`` (the
    historical ETL shape) and ``tools copy`` (address-to-address copy;
    added post-C1 to expose ``ovid2 tools copy`` on the web + MCP
    surfaces).

    Args:
        args: Parsed CLI arguments (from ``argparse``). Required
            attributes depend on ``args.command``; see
            :func:`_build_ovid2_argv`.
        instance: The per-invocation isolation context.

    Returns:
        Command tokens suitable for :func:`subprocess.run`.
    """
    project_root = Path(__file__).resolve().parents[3]
    output_dir = project_root / "output"
    output_dir.mkdir(exist_ok=True)

    cluster_mode = SPARK_MODE == "cluster" and not getattr(args, "no_spark_cluster", False)
    spark_config = effective_spark_config(cluster_mode=cluster_mode)
    driver_memory = spark_config.get("spark.driver.memory", "32g")
    driver_overhead = spark_config.get("spark.driver.memoryOverhead", "4g")
    docker_memory_gb = int(_parse_memory_gb(driver_memory) + _parse_memory_gb(driver_overhead) + 4)

    # DuckDB runs the whole job in-process in this one container, so it
    # is sized from the host (75% RAM, every CPU), not from Spark's
    # driver-heap cap, and the job's *Workers* field is its table
    # concurrency. See :func:`ovid2.config.spark_sizing.duckdb_container_sizing`.
    duckdb_env_flags: list[str] = []
    cpu_flags: list[str] = []
    if _is_duckdb_engine(args):
        from ovid2.config import spark_sizing

        sizing = spark_sizing.duckdb_container_sizing(getattr(args, "workers", None))
        docker_memory_gb = sizing["memory_gb"]
        cpu_flags = [f"--cpus={sizing['cpus']}"]
        duckdb_env_flags = [
            "-e",
            f"OVID2_DUCKDB_THREADS={os.environ.get('OVID2_DUCKDB_THREADS') or sizing['threads']}",
            "-e",
            "OVID2_DUCKDB_TABLE_CONCURRENCY="
            f"{os.environ.get('OVID2_DUCKDB_TABLE_CONCURRENCY') or sizing['table_concurrency']}",
        ]

    if cluster_mode:
        master_url = instance.spark_master_url()
        network_args = ["--network", instance.network_name]
    else:
        master_url = f"local[{SPARK_CORES}]"
        network_args = []

    # ``ovid2 etl`` doesn't define ``--master`` / ``--conf`` / ``--deploy-mode``
    # — those are spark-submit flags. Route them through env vars instead:
    # ``SPARK_MASTER_URL`` is picked up by :class:`PySparkEngine`, and
    # ``SPARK_CONF_<KEY>=value`` is a widely-used convention consumable by
    # any downstream tooling that wants to layer conf on top. The
    # ``spark.driver.memory`` is a JVM-launch setting, so the local-mode
    # (host-sized) value travels as ``OVID2_SPARK_DRIVER_MEMORY`` which
    # :class:`PySparkEngine` layers over ``SPARK_CONFIG``.
    spark_env_flags: list[str] = [
        "-e",
        f"SPARK_MASTER_URL={master_url}",
        "-e",
        f"SPARK_LOCAL_DIRS={SPARK_LOCAL_DIR_IN_CONTAINER}",
        "-e",
        f"OVID2_SPARK_DRIVER_MEMORY={driver_memory}",
        # The DuckDB engine spills / stages into the same real-disk
        # per-run scratch mount Spark uses.
        "-e",
        f"OVID2_DUCKDB_SCRATCH_ROOT={SPARK_LOCAL_DIR_IN_CONTAINER}",
        # DuckDB sizes ``memory_limit`` from /proc/meminfo, which inside a
        # container reports the *host's* RAM — far above the ``--memory``
        # cap below, so it would be OOM-killed before it ever spills.
        # Give it ~85% of the container cap (the rest is Python/Arrow).
        "-e",
        f"OVID2_DUCKDB_MEMORY_LIMIT={duckdb_memory_limit(docker_memory_gb)}",
    ]
    for key, value in spark_config.items():
        # Env var names must be shell-safe; keys already contain only
        # ``[a-z0-9.]``, so replace dots with underscores.
        env_key = "SPARK_CONF_" + key.replace(".", "_").upper()
        spark_env_flags.extend(["-e", f"{env_key}={value}"])

    # Prepend the bind-mounted source tree to ``PYTHONPATH`` so the driver
    # imports the *live* ``ovid2`` package from ``/app/src`` rather than the
    # wheel that was baked into the image at build time. Without this, code
    # changes in the working tree would require an image rebuild to take
    # effect on the driver — surprising during iteration and easy to forget
    # when hot-fixing production. The image entrypoint's py4j PYTHONPATH
    # manipulation still runs on top of this, so pyspark keeps working.
    #
    # ``/app/pipelines`` hosts the ``ovid2_pipelines`` PEP 420 namespace
    # package (``pipelines/ovid2_pipelines/<name>/pipeline_settings.py`` etc.
    # per AGENTS.md). Without it on the PYTHONPATH the driver cannot
    # ``import_module('ovid2_pipelines.<pipeline>.…')`` and every table
    # would fail with ``ModuleNotFoundError: No module named 'ovid2_pipelines'``.
    pythonpath_env: list[str] = [
        "-e",
        # pyspark + py4j are regular site-packages in the image, so only the
        # bind-mounted source trees need adding.
        "PYTHONPATH=/app/src:/app/pipelines",
    ]

    # Bind-mount the host's XDG config tree so the driver sees the same
    # address configs, secrets, connections, engine specs, and settings as
    # the host-side CLI. The image's ``ovid2`` user has
    # ``HOME=/docker_data/spark_tmp`` and an empty ``XDG_CONFIG_HOME``, so
    # without this mount the driver would search
    # ``/docker_data/spark_tmp/.config/ovid2/…`` (empty) and fail with a
    # ``FileNotFoundError`` from ``load_address_config`` — the exact error
    # observed before this change was in place. We mount at a fixed
    # container path (``/ovid2-user-config``) so the value of
    # ``XDG_CONFIG_HOME`` is deterministic across hosts.
    host_xdg_config = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    xdg_env: list[str] = [
        "-e",
        "XDG_CONFIG_HOME=/ovid2-user-config",
    ]
    xdg_mount: list[str] = []
    secret_scope_mounts: list[str] = []
    if host_xdg_config.is_dir():
        xdg_mount = ["-v", f"{host_xdg_config}:/ovid2-user-config:ro"]
        # Executor least-privilege (harness/API/executor trust model):
        # the full config tree above exposes *every* pipeline's
        # ``secrets.d/*.json`` at ``/ovid2-user-config/secrets.d``. Shadow
        # that directory with an empty tmpfs, then bind-mount back only
        # the secret files this run's addresses actually reference. A
        # compromise of one job is thus bounded to its own connections'
        # credentials, not the whole fleet. See
        # the adr-harness-api-executor-trust-model design note.
        secret_scope_mounts = _scoped_secret_mounts(args, host_xdg_config)

    # Bind-mount the host's per-invocation ``WORK_DIR`` (the JSONL event
    # log's home; ``$XDG_RUNTIME_DIR/ovid2`` by default) at the same
    # absolute path *and* propagate ``OVID2_WORK_DIR`` so the container-
    # side ``ovid2.config.settings.WORK_DIR`` resolves to the identical
    # directory. Without this, the driver's
    # :class:`~ovid2.display.bus.JsonlEventBus` writes
    # ``events-<id>.jsonl`` (and every ``engine.metrics`` sample the
    # live sampler emits) into the container's own
    # ``/tmp/ovid2-<uid>/ovid2/`` — a path invisible to the host's web
    # tier, which tails the file at the host's WORK_DIR. Result: the
    # run-detail page's live tiles stay dark for the entire job because
    # the file it's polling never appears on disk. Mounting the same
    # absolute path from both sides keeps
    # ``instance.event_log ==`` (host view) ``==`` (container view)
    # by construction, so no path translation is needed in the sampler
    # or the metrics service. ``ensure_directories()`` (called on the
    # host below via :attr:`OvidInstance.ensure_directories`) creates
    # WORK_DIR so docker doesn't reject the bind source as missing.
    work_env: list[str] = ["-e", f"OVID2_WORK_DIR={WORK_DIR}"]
    work_mount: list[str] = ["-v", f"{WORK_DIR}:{WORK_DIR}:rw"]

    # Snowflake connections continue to point at the legacy Ovid credential
    # directory (``/usr/include/rsa/``, containing ``rsa_key.p8`` and the
    # ``credentials`` passphrase file). Bind-mount that directory read-only
    # into the driver so ``SnowflakeConnection._build_connect_kwargs`` can
    # find the key at its declared path; without this the connector emits
    # ``Snowflake private key unavailable: … No such file or directory``
    # and downstream Snowflake I/O fails with an auth error.
    legacy_rsa_dir = Path("/usr/include/rsa")
    rsa_mount: list[str] = []
    if legacy_rsa_dir.is_dir():
        rsa_mount = ["-v", "/usr/include/rsa:/usr/include/rsa:ro"]

    # ``LEGACY_CREDENTIALS_DIR`` (``/ovid_credentials`` on legacy hosts, the
    # XDG fallback on developer laptops) only gets mounted when the source
    # actually exists — docker rejects the ``docker run`` invocation
    # otherwise, and hosts that use ``/usr/include/rsa`` above (or no
    # credentials at all) should not be blocked by an absent
    # ``/ovid_credentials`` tree.
    creds_mount: list[str] = []
    if Path(LEGACY_CREDENTIALS_DIR).is_dir():
        creds_mount = ["-v", f"{LEGACY_CREDENTIALS_DIR}:/ovid_credentials:ro"]

    # ``/data`` is read-only for the ETL family (etl / analysis /
    # dq_checks) — the pipeline reads from ``/data/inbox`` and writes
    # to Snowflake or another externally-provisioned sink, never back
    # to ``/data``. Enforcing this at mount time prevents accidental
    # mutation of the input drop by a mis-authored formatter or fixer.
    #
    # ``tools copy`` (and its driver-native successor ``tools move``)
    # legitimately writes to whatever the destination address points
    # at, which for disc-family destinations is under ``/data``. Both
    # kinds explicitly bypass formatter/mapper/fixer stages so the
    # read-only contract has nothing to protect here — mount the tree
    # read-write for both. ``health`` may refresh ``/data`` too. The
    # decision is registry-driven: a kind's
    # :attr:`~ovid2.run_kinds.KindSpec.read_only_data_mount` flag is the
    # single source of truth (see :mod:`ovid2.run_kinds`). An unknown /
    # missing command falls back to read-only — never widen to rw.
    from ovid2.run_kinds import kind_for_command

    _spec = kind_for_command(getattr(args, "command", "etl"))
    data_mount_mode = "ro" if (_spec is None or _spec.read_only_data_mount) else "rw"

    # Extra bind mounts flow from two sources, both landing on
    # ``args.parsed_extra_mounts`` before we get here:
    #
    # 1. Operator-supplied ``--extra-mount`` (copy kind only). The
    #    CLI's argparse hook parses and validates these before
    #    dispatch.
    # 2. Auto-mounts derived from disc connections referenced by
    #    ``--from`` / ``--to`` / ``--input_address`` /
    #    ``--output_address``. Applied to every kind so an ETL job
    #    that reads from a disc feed outside ``/data`` still has a
    #    valid mount root inside the container.
    #
    # Both are already validated (parse_extra_mount / disc_mount_for_connection)
    # so we can splat them here without re-checking.
    extra_mount_args: list[str] = []
    for m in getattr(args, "parsed_extra_mounts", []) or []:
        extra_mount_args.extend(["-v", m.as_docker_v()])

    # Run the container as the invoking host user so the read-only XDG
    # mount (secrets.d/*.json, mode 0o600 per AGENTS.md §7) is
    # readable inside the container. The image's default ``USER 1001``
    # would trip a ``PermissionError [Errno 13]`` on any secret owned
    # by the host user. See :func:`nss_wrapper_env` for the passwd
    # overlay that keeps Java's ``user.home`` resolvable under the
    # override.
    host_uid, host_gid = _host_user()
    user_flags: list[str] = ["--user", f"{host_uid}:{host_gid}"]
    nss_env_flags, nss_mount_flags = nss_wrapper_env(instance)

    # Bind-mount the per-invocation Spark scratch dirs onto
    # ``$SPARK_HOME/{work,logs}`` for the same reason the
    # master + workers do (see :func:`ovid2.core.compose._volume_mounts`
    # for the full rationale). The driver itself only writes here in
    # local Spark mode, but a plain ``ovid2 tools test-engine`` inside
    # the driver image would trip the same
    # ``IOException: Failed to create directory`` error the workers
    # do without the mount. ``ensure_directories()`` creates the host
    # paths first so ``docker run`` doesn't reject them as missing.
    instance.ensure_directories()
    spark_scratch_mounts: list[str] = [
        "-v",
        f"{instance.spark_work_dir}:{SPARK_WORK_DIR_IN_CONTAINER}:rw",
        "-v",
        f"{instance.spark_logs_dir}:{SPARK_LOGS_DIR_IN_CONTAINER}:rw",
        "-v",
        f"{instance.spark_local_dir}:{SPARK_LOCAL_DIR_IN_CONTAINER}:rw",
    ]

    cmd: list[str] = [
        "docker",
        "run",
        "--rm",
        "--name",
        driver_container_name(instance.instance_id),
        *user_flags,
        f"--memory={docker_memory_gb}g",
        *cpu_flags,
        "-e",
        f"OVID2_INSTANCE_ID={instance.instance_id}",
        *duckdb_env_flags,
        *pythonpath_env,
        *xdg_env,
        *work_env,
        *nss_env_flags,
        *spark_env_flags,
        *network_args,
        "-v",
        f"{project_root}:/app",
        "-v",
        f"{DATA_DIR}:/data:{data_mount_mode}",
        *extra_mount_args,
        *creds_mount,
        *xdg_mount,
        *secret_scope_mounts,
        *work_mount,
        *rsa_mount,
        *nss_mount_flags,
        *spark_scratch_mounts,
        "-w",
        "/app",
        DOCKER_IMAGE,
        # The image ENTRYPOINT (``/usr/local/bin/ovid2-entrypoint``) does
        # ``exec ovid2 "$@"``, so we pass ONLY the subcommand + flags here.
        # Prepending ``python -m ovid2`` would give ``ovid2 python -m ovid2 …``
        # and argparse would reject ``python`` as an unknown subcommand
        # (silent exit 2 with no pipeline execution).
        *_build_ovid2_argv(args),
    ]
    return cmd


def _build_ovid2_argv(args: Any) -> list[str]:
    """Return the ``ovid2 …`` argv tokens for the container ENTRYPOINT.

    Dispatches on ``args.command``:

    - ``etl`` / ``analysis`` / ``dq_checks`` — the historical ETL shape:
      ``--input_address``, ``--output_address``, ``--pipeline``,
      ``--batch_id``, ``--tables``, ``--transformations``, ``--engine``,
      plus optional ``--sf_database`` / ``--sf_account`` /
      ``--sf_schema``.
    - ``tools copy`` (or ``copy``) — the one-shot copy shape: ``--from``,
      ``--to``, ``--pipeline``, ``--batch_id``, ``--tables``,
      ``--engine``. No ``--transformations`` (copy has no
      formatter/mapper/fixer chain), no ``--sf_*`` (address configs
      supply Snowflake credentials directly), no ``--workers``.

    Splitting this out of :func:`build_docker_command` lets the docker-
    dispatch layer accept new nested subcommands (like ``tools copy``)
    without teaching the outer ``docker run`` builder about every flag
    surface. Backwards-compatible: existing ``args.command == 'etl'``
    call sites are unaffected.

    Args:
        args: Parsed CLI arguments. Attributes accessed depend on
            ``args.command`` — see the dispatch table above.

    Returns:
        A list of tokens starting with the subcommand name (nested
        subcommands like ``tools copy`` return two leading tokens).
    """
    command = getattr(args, "command", "etl")
    if command in ("tools export-table", "export"):
        return _build_tools_export_argv(args)
    if command in ("tools copy", "copy"):
        tokens = _build_tools_copy_argv(args)
    elif command in ("tools move", "move"):
        tokens = _build_tools_move_argv(args)
    elif command in ("health", "dq_checks"):
        tokens = _build_health_argv(args)
    else:
        tokens = _build_etl_argv(args, command)
    # The address is the exact write location: an operator-confirmed
    # overwrite must survive the host → container hop, otherwise the
    # in-container overwrite gate would refuse the run.
    if getattr(args, "overwrite", False):
        tokens.append("--overwrite")
    return tokens


def _build_etl_argv(args: Any, command: str) -> list[str]:
    """Return the ovid2 argv tokens for the etl-shaped subcommands."""
    tokens: list[str] = [
        command,
        "--input_address",
        args.input_address,
        "--output_address",
        args.output_address,
        "--pipeline",
        args.pipeline,
        "--batch_id",
        args.batch_id,
        "--tables",
        *args.tables,
        "--transformations",
        *args.transformations,
        "--engine",
        args.engine,
    ]
    for optional in ("sf_database", "sf_account", "sf_schema"):
        value = getattr(args, optional, None)
        if value:
            tokens.extend([f"--{optional}", value])
    return tokens


def _build_health_argv(args: Any) -> list[str]:
    """Return the ovid2 argv tokens for the health-shaped subcommands.

    Serves both ``ovid2 health`` and ``ovid2 dq_checks`` — the latter
    reuses the health check machinery but keeps its own leading token so
    the CLI parser accepts it and ``/data`` stays read-only. Health has no
    formatter/mapper/fixer chain, so it carries no ``--transformations``.
    It adds ``--packages`` / ``--parts`` / ``--checks`` (check selection)
    and an optional ``--prev_input_address`` (freshness panel).
    """
    tokens: list[str] = [
        getattr(args, "command", "health") or "health",
        "--pipeline",
        args.pipeline,
        "--batch_id",
        args.batch_id,
        "--engine",
        args.engine,
    ]
    # Endpoints: forward either the saved address name or the run-time
    # storage selection (connection + location). At least one form of
    # each is present (the CLI/API validates that upstream).
    for role in ("input", "output"):
        address = getattr(args, f"{role}_address", None)
        connection = getattr(args, f"{role}_connection", None)
        location = getattr(args, f"{role}_location", None)
        if address:
            tokens.extend([f"--{role}_address", address])
        if connection:
            tokens.extend([f"--{role}_connection", connection])
            if location:
                tokens.extend([f"--{role}_location", location])
    packages = getattr(args, "packages", None)
    if packages:
        tokens.extend(["--packages", *packages])
    parts = getattr(args, "parts", None)
    if parts:
        tokens.extend(["--parts", *parts])
    checks = getattr(args, "checks", None)
    if checks:
        tokens.extend(["--checks", *checks])
    prev = getattr(args, "prev_input_address", None)
    if prev:
        tokens.extend(["--prev_input_address", prev])
    cdm = getattr(args, "cdm", None)
    if cdm:
        tokens.extend(["--cdm", cdm])
    for optional in ("sf_database", "sf_account", "sf_schema"):
        value = getattr(args, optional, None)
        if value:
            tokens.extend([f"--{optional}", value])
    return tokens


def _build_tools_export_argv(args: Any) -> list[str]:
    """Return the ovid2 argv tokens for ``ovid2 tools export-table``.

    Runs the table export inside the driver container (row data never
    passes through a host process). ``--out`` is the same absolute path
    on both sides — :func:`ovid2.tools.table_ops._run_export_host_mode`
    bind-mounts its parent directory read-write.
    """
    tokens: list[str] = [
        "tools",
        "export-table",
        str(args.connection),
        "--path",
        str(args.path),
        "--format",
        str(args.fmt),
        "--delimiter",
        str(args.delimiter),
        "--header-case",
        str(args.header_case),
        "--out",
        str(args.out),
    ]
    if getattr(args, "limit", None) is not None:
        tokens.extend(["--limit", str(args.limit)])
    return tokens


def _build_tools_copy_argv(args: Any) -> list[str]:
    """Return the ovid2 argv tokens for ``ovid2 tools copy``.

    ``args`` may expose either the ``ovid2 tools copy`` flag names
    (``from_address``, ``to_address``) or the etl-adjacent names
    (``input_address``, ``output_address``). Both are accepted so
    callers built for the etl shape (the web worker) don't need to
    duplicate the address into two attributes.
    """
    from_addr = getattr(args, "from_address", None) or args.input_address
    to_addr = getattr(args, "to_address", None) or args.output_address
    tokens: list[str] = [
        "tools",
        "copy",
        "--from",
        from_addr,
        "--to",
        to_addr,
        "--pipeline",
        args.pipeline,
        "--batch_id",
        args.batch_id,
        "--tables",
        *args.tables,
        "--engine",
        args.engine,
    ]
    # Forward every ``--extra-mount`` spec so the container-side CLI
    # sees the same argv as the host-side one. The mount itself is
    # already applied by ``docker run -v`` / the compose ``volumes:``
    # list; passing the flag here only preserves round-trip symmetry
    # for logging and display.
    for m in getattr(args, "parsed_extra_mounts", []) or []:
        tokens.extend(["--extra-mount", m.as_docker_v()])
    return tokens


def _build_tools_move_argv(args: Any) -> list[str]:
    """Return the ovid2 argv tokens for ``ovid2 tools move``.

    Mirrors :func:`_build_tools_copy_argv`: same flag set plus the
    boolean ``--strict-types`` that :mod:`ovid2.tools.move` accepts.
    ``args`` may expose either the ``ovid2 tools move`` flag names
    (``from_address``, ``to_address``) or the etl-adjacent names
    (``input_address``, ``output_address``); both are accepted so the
    web worker and MCP layer can dispatch either shape without
    duplicating fields.
    """
    from_addr = getattr(args, "from_address", None) or args.input_address
    to_addr = getattr(args, "to_address", None) or args.output_address
    tokens: list[str] = [
        "tools",
        "move",
        "--from",
        from_addr,
        "--to",
        to_addr,
        "--pipeline",
        args.pipeline,
        "--batch_id",
        args.batch_id,
        "--tables",
        *args.tables,
        "--engine",
        args.engine,
    ]
    if getattr(args, "strict_types", False):
        tokens.append("--strict-types")
    for m in getattr(args, "parsed_extra_mounts", []) or []:
        tokens.extend(["--extra-mount", m.as_docker_v()])
    return tokens


def stop_driver_container(instance_id: str, *, timeout: int = 35) -> None:
    """Stop the per-invocation driver container by name (best-effort).

    Sends SIGTERM to the container's PID 1 via ``docker stop`` (with a
    grace period, then SIGKILL). This is the *only* path by which a
    host-side cancel reaches the in-container ``ovid2 etl`` process — so
    its SIGTERM handler can run (e.g. Snowpark's
    ``SYSTEM$CANCEL_ALL_QUERIES``) before the container exits. Merely
    killing the host-side ``docker run`` client does not signal the
    container.

    ``docker stop`` on an already-gone / never-started container is a
    no-op error we swallow — a job cancelled while still queued, or a
    local-mode run where the container already exited, must not raise.

    The grace period is deliberately a few seconds longer than the
    in-container handler's own teardown budget so the Snowflake cancel
    round-trip has time to complete before docker escalates to SIGKILL.
    """
    name = driver_container_name(instance_id)
    try:
        subprocess.run(
            ["docker", "stop", "--timeout", str(int(timeout)), name],
            capture_output=True,
            check=False,
            timeout=timeout + 15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(  # noqa: T201 — host-side CLI is permitted to print
            f"[ovid2] driver container stop for {name} reported: {exc}",
            file=sys.stderr,
        )


def engine_needs_spark_cluster(engine: Any) -> bool:
    """Return ``True`` when ``engine`` computes on a Spark cluster.

    Non-Spark engines (Snowpark: compute runs inside Snowflake) only
    need a single driver container — starting a per-invocation Spark
    master + workers for them is pure overhead. Unknown / ``None``
    engines default to ``True`` (the historical behaviour).
    """
    if engine is None:
        return True
    return bool(getattr(engine, "session_is_spark", True))


def submit_one_shot(
    args: Any,
    instance: OvidInstance,
    *,
    spark_cluster: bool,
    spark_mode: str | None = None,
) -> int:
    """Dispatch a one-shot tool (copy / move) to Docker.

    Uses :func:`submit_with_cluster` when ``SPARK_MODE == "cluster"``
    and ``spark_cluster`` is ``True``; otherwise runs a single driver
    container via :func:`submit_to_docker`. When the cluster is skipped
    because the engine is not Spark-family, ``args.no_spark_cluster``
    is set so :func:`build_docker_command` emits neither the instance
    network nor the cluster master URL.

    Args:
        args: Parsed CLI arguments (host-mode shape).
        instance: The per-invocation isolation context.
        spark_cluster: Whether the selected engine needs a Spark cluster
            (see :func:`engine_needs_spark_cluster`).
        spark_mode: Override for ``settings.SPARK_MODE`` (callers pass
            the value they read so tests can patch it in one place).

    Returns:
        The subprocess return code.
    """
    mode = SPARK_MODE if spark_mode is None else spark_mode
    if mode == "cluster" and spark_cluster:
        return submit_with_cluster(args, instance)
    if not spark_cluster:
        args.no_spark_cluster = True
        print(  # noqa: T201 — host-side CLI is permitted to print
            f"[ovid2] Engine '{getattr(args, 'engine', '?')}' is not Spark-family; "
            f"running a single driver container (no Spark cluster, id={instance.instance_id})."
        )
    return submit_to_docker(args, instance)


def submit_to_docker(args: Any, instance: OvidInstance) -> int:
    """Submit an ETL job to a single Docker container (local Spark mode).

    Args:
        args: Parsed CLI arguments.
        instance: The per-invocation isolation context.

    Returns:
        The subprocess return code.
    """
    prev_sigterm = None
    prev_sigint = None

    def _signal_handler(signum: int, _frame: FrameType | None) -> None:
        sig_name = signal.Signals(signum).name
        print(  # noqa: T201
            f"[ovid2] Received {sig_name}; stopping driver container "
            f"(id={instance.instance_id})...",
            file=sys.stderr,
        )
        # SIGTERM the container's PID 1 so the in-container ovid2 signal
        # handler runs (Snowflake query cancel etc.) before it exits.
        stop_driver_container(instance.instance_id)
        raise SystemExit(1)

    try:
        prev_sigterm = signal.signal(signal.SIGTERM, _signal_handler)
        prev_sigint = signal.signal(signal.SIGINT, _signal_handler)
        cmd = build_docker_command(args, instance)
        result = subprocess.run(cmd, check=False)
        return result.returncode
    finally:
        if prev_sigterm is not None:
            signal.signal(signal.SIGTERM, prev_sigterm)
        if prev_sigint is not None:
            signal.signal(signal.SIGINT, prev_sigint)


def _remove_instance_network(instance: OvidInstance) -> None:
    """Best-effort removal of an instance's docker network.

    Called when ``docker compose up`` fails after partially creating the
    project's network, so a failed bring-up does not leak its subnet from
    Docker's finite default address pool. A ``docker network rm`` on a
    network that never existed (or still has attached containers) is a
    harmless no-op / error that we swallow — the periodic reaper
    (:func:`ovid2.core.isolation._reap_orphaned_networks`) is the
    backstop for anything this misses.

    Args:
        instance: The per-invocation isolation context.
    """
    try:
        subprocess.run(
            ["docker", "network", "rm", instance.network_name],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug(
            "Could not remove network %s after failed bring-up: %s",
            instance.network_name,
            exc,
        )


def start_spark_cluster(
    instance: OvidInstance,
    num_workers: int,
    *,
    data_rw: bool = False,
    extra_mounts: list[Any] | None = None,
) -> bool:
    """Start the Spark cluster for the given instance.

    Generates the instance-scoped docker-compose file, brings the services
    up with ``docker compose up -d``, then polls the master UI until it
    responds or the retry budget is exhausted.

    Args:
        instance: The per-invocation isolation context.
        num_workers: Number of Spark worker containers to start.
        data_rw: When ``True``, mount ``/data`` read-write in every
            container. Reserved for the ``copy`` kind whose destination
            address may live under ``/data``; every other kind keeps
            the ETL read-only contract. Forwarded to
            :func:`ovid2.core.compose.generate_compose_file`.
        extra_mounts: List of parsed :class:`ovid2.tools.extra_mounts.ExtraMount`
            entries (copy-kind only). Applied to master and every worker.

    Returns:
        ``True`` if the cluster reported ready, ``False`` on timeout.
    """
    # Lazy import so ``pyyaml`` is only needed on hosts that actually start clusters.
    from ovid2.core.compose import generate_compose_file

    msg = f"[ovid2] Starting Spark cluster ({num_workers} workers, id={instance.instance_id})..."
    print(msg)  # noqa: T201 — host-side CLI is permitted to print
    generate_compose_file(
        instance,
        num_workers,
        data_rw=data_rw,
        extra_mounts=extra_mounts,
    )

    try:
        subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(instance.compose_file),
                "-p",
                instance.project_name,
                "up",
                "-d",
            ],
            check=True,
        )
    except subprocess.CalledProcessError:
        # ``compose up`` can fail after partially creating the project's
        # network (a container failed to start, or — when the address
        # pool is nearly exhausted — a sibling resource errored). Best-
        # effort remove the instance network so a failed bring-up never
        # leaks its subnet from Docker's finite default address pool.
        _remove_instance_network(instance)
        raise

    max_retries = 30
    for _ in range(max_retries):
        # ``curl`` is not installed in the runtime image the master and
        # workers run; use ``wget`` (which the compose healthcheck already
        # relies on) to probe the master UI instead. ``-q`` silences the
        # progress meter; ``-O -`` prints the body to stdout; we grep for
        # the well-known "Spark Master" title of the UI landing page.
        result = subprocess.run(
            [
                "docker",
                "exec",
                instance.master_name,
                "bash",
                "-c",
                "wget -q -O - http://localhost:8080 | grep -q 'Spark Master'",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            print("[ovid2] Spark cluster ready.")  # noqa: T201
            time.sleep(3)
            return True
        time.sleep(1)

    print("[ovid2] Cluster not fully ready after timeout; proceeding anyway.", file=sys.stderr)  # noqa: T201
    return False


def stop_spark_cluster(instance: OvidInstance) -> None:
    """Stop and clean up the Spark cluster for the given instance.

    Runs ``docker compose down --remove-orphans`` with a 30s timeout;
    retries with 60s + ``--volumes`` on failure. Always calls
    :meth:`OvidInstance.cleanup` at the end.
    """
    if not instance.compose_file.exists():
        instance.cleanup()
        return

    print(f"[ovid2] Stopping Spark cluster (id={instance.instance_id})...")  # noqa: T201
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(instance.compose_file),
            "-p",
            instance.project_name,
            "down",
            "--remove-orphans",
            "--timeout",
            "30",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        print(  # noqa: T201
            "[ovid2] Shutdown reported errors; retrying with extended timeout.",
            file=sys.stderr,
        )
        subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(instance.compose_file),
                "-p",
                instance.project_name,
                "down",
                "--remove-orphans",
                "--volumes",
                "--timeout",
                "60",
            ],
            capture_output=True,
            check=False,
        )
    instance.cleanup()


def submit_with_cluster(args: Any, instance: OvidInstance | None = None) -> int:
    """Start an isolated Spark cluster, run the ETL job, then stop the cluster.

    Args:
        args: Parsed CLI arguments (must include ``workers``).
        instance: Optional isolation context; a fresh one is created if omitted.

    Returns:
        The subprocess return code from ``spark-submit``.
    """
    if instance is None:
        instance = OvidInstance()

    prev_sigterm = None
    prev_sigint = None
    return_code = 0

    def _signal_handler(signum: int, _frame: FrameType | None) -> None:
        sig_name = signal.Signals(signum).name
        print(  # noqa: T201
            f"[ovid2] Received {sig_name}; shutting down cluster (id={instance.instance_id})...",
            file=sys.stderr,
        )
        # Stop the driver container FIRST so its in-container ovid2
        # signal handler runs (e.g. Snowpark SYSTEM$CANCEL_ALL_QUERIES)
        # and it exits gracefully, before we tear the cluster down
        # underneath it.
        stop_driver_container(instance.instance_id)
        stop_spark_cluster(instance)
        raise SystemExit(1)

    try:
        prev_sigterm = signal.signal(signal.SIGTERM, _signal_handler)
        prev_sigint = signal.signal(signal.SIGINT, _signal_handler)

        # Registry-driven: mount ``/data`` read-write for kinds whose
        # spec opts out of the read-only invariant (copy / move / health).
        from ovid2.run_kinds import kind_for_command

        _spec = kind_for_command(getattr(args, "command", "etl"))
        data_rw = _spec is not None and not _spec.read_only_data_mount
        start_spark_cluster(
            instance,
            args.workers,
            data_rw=data_rw,
            extra_mounts=getattr(args, "parsed_extra_mounts", None),
        )
        cmd = build_docker_command(args, instance)
        result = subprocess.run(cmd, check=False)
        return_code = result.returncode
    finally:
        stop_spark_cluster(instance)
        if prev_sigterm is not None:
            signal.signal(signal.SIGTERM, prev_sigterm)
        if prev_sigint is not None:
            signal.signal(signal.SIGINT, prev_sigint)
    return return_code
