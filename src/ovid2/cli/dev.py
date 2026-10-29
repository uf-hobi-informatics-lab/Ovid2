"""``ovid2 dev`` — isolated per-developer Ovid2 instances on a shared host.

Several developers working on one server each need their own running
Ovid2 (web panel + job worker, optionally the MCP server) that never
touches anyone else's state. ``ovid2 dev up`` provisions one:

* **Own state tree.** Every XDG root (config, state, cache, data,
  runtime) is redirected into ``~/.local/share/ovid2-dev/<name>/`` (or
  ``$OVID2_DEV_HOME/<name>``), so ``scheduler.db``, ``settings.json``,
  ``secrets.d``, connections, engines, logs, the run work dir and the
  event archive are all private to the instance. Processes run with the
  instance root as their working directory, which neutralises the
  ``./configs`` / ``./connections`` / ``./engines`` cwd fallbacks.
* **Own ports.** A web port and the MCP port right after it are leased
  from a host-wide registry (``/var/tmp/ovid2-dev/ports`` by default,
  ``$OVID2_DEV_REGISTRY`` to override; range ``8100-8199`` by default,
  ``$OVID2_DEV_PORT_RANGE``). Leases are created with ``O_EXCL`` so two
  developers starting at the same moment can never get the same port;
  a lease whose processes are all dead is reclaimed.
* **Own Docker image tag.** ``OVID2_DOCKER_IMAGE`` defaults to
  ``ovid2:dev-<user>`` so ``ovid2 image build`` inside a dev instance
  never overwrites the shared production tag. On first use the tag is
  seeded from the shared image with ``docker tag`` (no rebuild).
* **Own session cookie.** ``settings.auth.cookie_name`` is made unique
  per instance, since browsers do not scope cookies by port.

The instance runs the *current interpreter* (``sys.executable``), so a
developer's editable install — i.e. their own checkout — is the code
that runs.

Subcommands: ``up``, ``down``, ``restart``, ``ls``, ``logs``, ``env``.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import getpass
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_PORT_RANGE",
    "Lease",
    "acquire_lease",
    "default_host",
    "instance_env",
    "instance_root",
    "list_leases",
    "port_range",
    "registry_dir",
    "release_lease",
]

#: Default inclusive port range handed out to dev instances. Production
#: keeps its own port outside this range.
DEFAULT_PORT_RANGE: tuple[int, int] = (8100, 8199)

#: Seconds a lease without live processes is still considered "starting".
_STARTING_GRACE_S: float = 120.0

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

#: Inherited ``OVID2_*`` variables that would punch through the instance's
#: isolation (they point at someone else's state or ports). They are
#: dropped from the child environment before the instance values are set.
_LEAKY_ENV: tuple[str, ...] = (
    "OVID2_SCHEDULER_DB",
    "OVID2_SETTINGS_FILE",
    "OVID2_CONFIGS_DIR",
    "OVID2_CONNECTIONS_DIR",
    "OVID2_ROUTES_DIR",
    "OVID2_ENGINES_DIR",
    "OVID2_LLMS_DIR",
    "OVID2_PIPELINES_DIR",
    "OVID2_WEB_LOG_DIR",
    "OVID2_WEB_EXPORT_DIR",
    "OVID2_SECRETS_FILE",
    "OVID2_SEED_FILE",
    "OVID2_ARCHIVE_DIR",
    "OVID2_WORK_DIR",
    "OVID2_ACCESS_MIRROR_DIR",
    "OVID2_WEB_BIND",
    "OVID2_MCP_BIND",
    "OVID2_WEB_URL",
    "OVID2_LLM_TOOLS_API_URL",
    "OVID2_MCP_API_TOKEN",
    "OVID2_DOCKER_IMAGE",
)

_XDG_LAYOUT: tuple[tuple[str, str], ...] = (
    ("XDG_CONFIG_HOME", "config"),
    ("XDG_STATE_HOME", "state"),
    ("XDG_CACHE_HOME", "cache"),
    ("XDG_DATA_HOME", "data"),
    ("XDG_RUNTIME_DIR", "run"),
)

_PROCESSES: tuple[str, ...] = ("web", "worker", "mcp")


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


def current_user() -> str:
    """Return the invoking user's login name."""
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return str(os.getuid())


def _slug(value: str) -> str:
    """Return ``value`` reduced to ``[a-z0-9_.-]`` (Docker-tag / cookie safe)."""
    slug = re.sub(r"[^a-z0-9_.-]+", "-", value.lower()).strip(".-_")
    return slug or "user"


def validate_name(name: str) -> str:
    """Return ``name`` if it is a valid instance name, else raise ``ValueError``."""
    if not _NAME_RE.match(name):
        raise ValueError(
            f"invalid instance name {name!r}: use 1-32 chars of a-z, 0-9, '-', '_' "
            "starting with a letter or digit"
        )
    return name


def instances_home() -> Path:
    """Return the directory holding every dev instance of the current user.

    Deliberately *not* derived from ``$XDG_DATA_HOME``: inside an
    activated instance (``eval "$(ovid2 dev env)"``) that variable points
    into the instance itself, and instances must not nest.
    """
    override = os.environ.get("OVID2_DEV_HOME")
    if override:
        return Path(override)
    return Path.home() / ".local" / "share" / "ovid2-dev"


def instance_root(name: str) -> Path:
    """Return the root directory of instance ``name``."""
    return instances_home() / validate_name(name)


def registry_dir() -> Path:
    """Return the host-wide port-lease registry shared by all developers."""
    override = os.environ.get("OVID2_DEV_REGISTRY")
    if override:
        return Path(override)
    return Path("/var/tmp/ovid2-dev")  # noqa: S108 — shared, sticky, host-wide registry


def port_range() -> tuple[int, int]:
    """Return the inclusive ``(low, high)`` port range for dev instances.

    Read from ``$OVID2_DEV_PORT_RANGE`` (``"LOW-HIGH"``), falling back to
    :data:`DEFAULT_PORT_RANGE`.

    Raises:
        ValueError: When the env value is malformed.
    """
    raw = os.environ.get("OVID2_DEV_PORT_RANGE", "").strip()
    if not raw:
        return DEFAULT_PORT_RANGE
    low_s, sep, high_s = raw.partition("-")
    if not sep:
        raise ValueError(f"OVID2_DEV_PORT_RANGE={raw!r}: expected LOW-HIGH")
    low, high = int(low_s), int(high_s)
    if not 1024 <= low < high <= 65535:
        raise ValueError(f"OVID2_DEV_PORT_RANGE={raw!r}: need 1024 <= LOW < HIGH <= 65535")
    return low, high


def default_host() -> str:
    """Return the address dev instances bind to.

    ``$OVID2_DEV_HOST`` wins. Otherwise the host's primary LAN address is
    discovered by asking the kernel which source address it would use
    for an outbound UDP datagram (nothing is sent — UDP ``connect`` only
    selects a route), so teammates can open the panel from a browser.
    Falls back to loopback.
    """
    override = os.environ.get("OVID2_DEV_HOST", "").strip()
    if override:
        return override
    with (
        contextlib.suppress(OSError),
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe,
    ):
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1; route lookup only
        addr = str(probe.getsockname()[0])
        if addr and not addr.startswith("0."):
            return addr
    return "127.0.0.1"


def default_image() -> str:
    """Return the per-developer Docker image tag (``ovid2:dev-<user>``)."""
    return f"ovid2:dev-{_slug(current_user())}"


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def instance_env(
    root: Path,
    *,
    name: str,
    host: str,
    port: int,
    mcp_port: int,
    image: str,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the full child environment for an instance.

    Args:
        root: Instance root directory.
        name: Instance name.
        host: Bind address.
        port: Web port.
        mcp_port: MCP HTTP port.
        image: Docker image tag used for runs and ``ovid2 image build``.
        base: Environment to start from (defaults to ``os.environ``).

    Returns:
        A new mapping; ``base`` is not mutated.
    """
    env = dict(os.environ if base is None else base)
    for key in _LEAKY_ENV:
        env.pop(key, None)
    for var, sub in _XDG_LAYOUT:
        env[var] = str(root / sub)
    env.update(
        {
            "OVID2_DEV_INSTANCE": name,
            "OVID2_DEV_ROOT": str(root),
            "OVID2_WORK_DIR": str(root / "run" / "ovid2"),
            "OVID2_ARCHIVE_DIR": str(root / "state" / "ovid2" / "archive"),
            "OVID2_ACCESS_MIRROR_DIR": str(root / "cache" / "ovid2" / "access_mirror"),
            "OVID2_WEB_BIND": f"{host}:{port}",
            "OVID2_MCP_BIND": f"{host}:{mcp_port}",
            "OVID2_WEB_URL": f"http://{host}:{port}",
            "OVID2_DOCKER_IMAGE": image,
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


def cookie_name(name: str) -> str:
    """Return the instance-unique session-cookie name."""
    return f"ovid2_session_{_slug(current_user()).replace('.', '_')}_{name}".replace("-", "_")


# ---------------------------------------------------------------------------
# Port leases
# ---------------------------------------------------------------------------


@dataclass
class Lease:
    """A claimed (web, mcp) port pair in the shared registry."""

    port: int
    mcp_port: int
    user: str
    uid: int
    name: str
    root: str
    pids: list[int] = field(default_factory=list)
    created: float = field(default_factory=time.time)

    @property
    def path(self) -> Path:
        """Return the lease file path."""
        return _ports_dir() / f"{self.port}.json"

    def to_json(self) -> str:
        """Serialise the lease."""
        return json.dumps(self.__dict__, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_file(cls, path: Path) -> Lease | None:
        """Load a lease file, returning ``None`` when unreadable or malformed."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(**data)
        except (OSError, ValueError, TypeError):
            return None

    def alive(self) -> bool:
        """Return ``True`` while any leased process lives (or startup is in progress)."""
        if any(_pid_alive(pid) for pid in self.pids):
            return True
        return time.time() - self.created < _STARTING_GRACE_S and not self.pids


def _ports_dir() -> Path:
    """Return (creating if needed) the sticky, world-writable lease directory."""
    ports = registry_dir() / "ports"
    for directory in (registry_dir(), ports):
        if not directory.is_dir():
            directory.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(directory, 0o1777)  # noqa: S103 — sticky, like /tmp
    return ports


def _pid_alive(pid: int) -> bool:
    """Return ``True`` when ``pid`` exists (owned by anyone)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _port_bindable(host: str, port: int) -> bool:
    """Return ``True`` when nothing is listening on ``host:port``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def list_leases() -> list[Lease]:
    """Return every lease in the registry, sorted by port."""
    ports = registry_dir() / "ports"
    if not ports.is_dir():
        return []
    leases = [Lease.from_file(p) for p in sorted(ports.glob("*.json"))]
    return sorted((lease for lease in leases if lease), key=lambda lease: lease.port)


@contextlib.contextmanager
def _registry_lock() -> Iterator[None]:
    """Hold the host-wide registry ``flock`` (serialises claim / reclaim)."""
    lock = _ports_dir() / ".lock"
    # O_RDONLY is enough for flock and works whoever created the file.
    fd = os.open(lock, os.O_RDONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _try_claim(lease: Lease) -> bool:
    """Create the lease file, reclaiming a stale one; call under the lock.

    The file is written to a private temp name first and published with
    ``os.link`` (atomic, fails if the target exists), so readers never
    observe a half-written lease.
    """
    path = lease.path
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(lease.to_json(), encoding="utf-8")
    try:
        for _ in range(2):
            try:
                os.link(tmp, path)
            except FileExistsError:
                existing = Lease.from_file(path)
                if existing is not None and existing.alive():
                    return False
                try:
                    path.unlink()  # stale; the sticky dir only lets its owner delete
                except OSError:
                    return False
                continue
            return True
        return False
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def acquire_lease(
    *,
    name: str,
    root: Path,
    host: str,
    port: int | None = None,
    preferred: int | None = None,
) -> Lease:
    """Claim a (web, mcp) port pair.

    Args:
        name: Instance name recorded in the lease.
        root: Instance root recorded in the lease.
        host: Address the ports must be bindable on.
        port: Exact web port to claim (``--port``); no fallback.
        preferred: Port to try first (the instance's previous port).

    Returns:
        The claimed :class:`Lease`.

    Raises:
        RuntimeError: When the requested port (or every port in range) is taken.
    """
    low, high = port_range()
    if port is not None:
        candidates = [port]
    else:
        candidates = list(range(low + (low % 2), high, 2))  # even web, odd mcp
        if preferred in candidates:
            candidates.remove(preferred)
            candidates.insert(0, preferred)
    for candidate in candidates:
        if not (_port_bindable(host, candidate) and _port_bindable(host, candidate + 1)):
            continue
        lease = Lease(
            port=candidate,
            mcp_port=candidate + 1,
            user=current_user(),
            uid=os.getuid(),
            name=name,
            root=str(root),
        )
        with _registry_lock():
            if _try_claim(lease):
                return lease
    if port is not None:
        raise RuntimeError(f"port {port} (or {port + 1}) is in use or leased by someone else")
    raise RuntimeError(f"no free port pair left in {low}-{high}; see `ovid2 dev ls --all`")


def update_lease(lease: Lease) -> None:
    """Rewrite an owned lease (e.g. with the child PIDs)."""
    lease.path.write_text(lease.to_json(), encoding="utf-8")


def release_lease(port: int) -> None:
    """Delete the lease for ``port`` if it belongs to the current user."""
    path = _ports_dir() / f"{port}.json"
    existing = Lease.from_file(path)
    if existing is not None and existing.uid == os.getuid():
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


# ---------------------------------------------------------------------------
# Instance metadata
# ---------------------------------------------------------------------------


def _meta_path(root: Path) -> Path:
    return root / "instance.json"


def read_meta(root: Path) -> dict[str, Any]:
    """Return the instance's ``instance.json`` (empty dict when absent)."""
    try:
        data = json.loads(_meta_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_meta(root: Path, meta: Mapping[str, Any]) -> None:
    """Persist ``instance.json``."""
    _meta_path(root).write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", "utf-8")


def _is_ovid2_process(pid: int) -> bool:
    """Guard against PID reuse: only signal processes that look like ovid2."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        return False
    except OSError:
        return True  # no /proc: trust the recorded PID
    return b"ovid2" in cmdline


def running_pids(meta: Mapping[str, Any]) -> dict[str, int]:
    """Return ``{process: pid}`` for the instance processes still alive."""
    pids = meta.get("pids") or {}
    return {
        proc: int(pid)
        for proc, pid in pids.items()
        if isinstance(pid, int) and _pid_alive(pid) and _is_ovid2_process(pid)
    }


def _iter_instances() -> Iterator[tuple[str, Path]]:
    home = instances_home()
    if not home.is_dir():
        return
    for child in sorted(home.iterdir()):
        if _meta_path(child).is_file():
            yield child.name, child


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


def provision(root: Path, name: str, env: Mapping[str, str]) -> list[str]:
    """Create the instance tree on first use; idempotent afterwards.

    Runs ``ovid2 init`` under the instance environment (fresh XDG tree +
    sample address configs) and writes a ``settings.json`` carrying an
    instance-unique session-cookie name.

    Returns:
        Human-readable log lines.
    """
    log: list[str] = []
    fresh = not root.exists()
    root.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(root, 0o700)
    for _, sub in _XDG_LAYOUT:
        (root / sub).mkdir(exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    if fresh or not (root / "config" / "ovid2").is_dir():
        result = subprocess.run(
            [sys.executable, "-m", "ovid2.cli", "init"],
            cwd=root,
            env=dict(env),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ovid2 init failed:\n{result.stdout}{result.stderr}")
        log.append(f"initialised fresh instance tree at {root}")
    settings = root / "config" / "ovid2" / "settings.json"
    if not settings.exists():
        settings.parent.mkdir(parents=True, exist_ok=True)
        payload = {"auth": {"cookie_name": cookie_name(name)}}
        settings.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        log.append(f"wrote {settings} (cookie {cookie_name(name)})")
    return log


def _ensure_image(image: str) -> str | None:
    """Seed the per-dev image tag from the shared one; return a warning or ``None``."""
    docker = shutil.which("docker")
    if docker is None:
        return "docker not found on PATH — container runs will not work"
    probe = subprocess.run([docker, "image", "inspect", image], capture_output=True, check=False)
    if probe.returncode == 0:
        return None
    shared_image = os.environ.get("OVID2_DEV_BASE_IMAGE", "ovid2:latest")
    if shared_image == image:
        return f"image {image} not found — build it with `ovid2 image build`"
    tagged = subprocess.run(
        [docker, "tag", shared_image, image], capture_output=True, text=True, check=False
    )
    if tagged.returncode == 0:
        print(f"[ovid2 dev] seeded image {image} from {shared_image}")
        return None
    return (
        f"image {image} not found and could not tag it from {shared_image}; "
        f'run `eval "$(ovid2 dev env)" && ovid2 image build`'
    )


def _spawn(argv: list[str], *, root: Path, env: Mapping[str, str], log: Path) -> int:
    """Start a detached child process and return its PID."""
    with open(log, "ab") as handle:
        handle.write(f"\n===== {time.strftime('%F %T')} {' '.join(argv)}\n".encode())
        handle.flush()
        process = subprocess.Popen(
            argv,
            cwd=root,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return process.pid


def _wait_listening(host: str, port: int, pid: int, timeout: float) -> bool:
    """Poll until ``host:port`` accepts connections or ``pid`` dies."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return False
        with contextlib.suppress(OSError), socket.create_connection((host, port), timeout=1):
            return True
        time.sleep(0.5)
    return False


def _tail(path: Path, lines: int = 30) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def _stop_pids(pids: Mapping[str, int], timeout: float = 40.0) -> None:
    """SIGTERM each process group, then SIGKILL stragglers after ``timeout``."""
    for pid in pids.values():
        with contextlib.suppress(ProcessLookupError, PermissionError):
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(_pid_alive(p) for p in pids.values()):
        time.sleep(0.25)
    for pid in pids.values():
        if _pid_alive(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGKILL)


# ---------------------------------------------------------------------------
# Subcommand entries
# ---------------------------------------------------------------------------


def _url(meta: Mapping[str, Any]) -> str:
    return f"http://{meta.get('host')}:{meta.get('port')}"


def _cmd_up(args: argparse.Namespace) -> int:
    name = validate_name(args.name)
    root = instance_root(name)
    meta = read_meta(root)
    live = running_pids(meta)
    if live:
        print(f"[ovid2 dev] '{name}' already running at {_url(meta)} (pids {live})")
        return 0

    host = args.host or meta.get("host") or default_host()
    image = args.image or meta.get("image") or default_image()
    if meta.get("port"):
        release_lease(int(meta["port"]))
    try:
        lease = acquire_lease(
            name=name, root=root, host=host, port=args.port, preferred=meta.get("port")
        )
    except (RuntimeError, ValueError) as exc:
        print(f"[ovid2 dev] {exc}", file=sys.stderr)
        return 1

    env = instance_env(
        root, name=name, host=host, port=lease.port, mcp_port=lease.mcp_port, image=image
    )
    try:
        for line in provision(root, name, env):
            print(f"[ovid2 dev] {line}")
    except RuntimeError as exc:
        release_lease(lease.port)
        print(f"[ovid2 dev] {exc}", file=sys.stderr)
        return 1
    warning = _ensure_image(image)

    logs = root / "logs"
    py = [sys.executable, "-m", "ovid2.cli"]
    pids: dict[str, int] = {
        "web": _spawn(
            [*py, "web", "--bind", f"{host}:{lease.port}"], root=root, env=env, log=logs / "web.log"
        )
    }
    if not args.no_worker:
        pids["worker"] = _spawn([*py, "web-worker"], root=root, env=env, log=logs / "worker.log")
    if args.mcp:
        pids["mcp"] = _spawn(
            [*py, "mcp", "--transport", "http", "--bind", f"{host}:{lease.mcp_port}"],
            root=root,
            env=env,
            log=logs / "mcp.log",
        )
    lease.pids = list(pids.values())
    update_lease(lease)
    meta = {
        "name": name,
        "host": host,
        "port": lease.port,
        "mcp_port": lease.mcp_port,
        "image": image,
        "python": sys.executable,
        "pids": pids,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    write_meta(root, meta)

    if not _wait_listening(host, lease.port, pids["web"], args.timeout):
        print("[ovid2 dev] web server did not come up; last log lines:", file=sys.stderr)
        print(_tail(logs / "web.log"), file=sys.stderr)
        _stop_pids(pids, timeout=10)
        release_lease(lease.port)
        return 1

    print(f"[ovid2 dev] '{name}' is up")
    print(f"  web     {_url(meta)}")
    if "mcp" in pids:
        print(f"  mcp     http://{host}:{lease.mcp_port}")
    print(f"  worker  {'pid ' + str(pids['worker']) if 'worker' in pids else 'not started'}")
    print(f"  state   {root}")
    print(f"  image   {image}")
    print(f"  logs    ovid2 dev logs {name} -f")
    print(f'  shell   eval "$(ovid2 dev env {name})"   # run ovid2 CLI against this instance')
    if warning:
        print(f"[ovid2 dev] WARNING: {warning}")
    if not host.startswith("127."):
        print(
            f"[ovid2 dev] NOTE: bound to {host} with auth off — anyone on the network can "
            "use this panel. Use --host 127.0.0.1 or enable auth for sensitive connections."
        )
    return 0


def _cmd_down(args: argparse.Namespace) -> int:
    name = validate_name(args.name)
    root = instance_root(name)
    meta = read_meta(root)
    if not meta:
        print(f"[ovid2 dev] no instance named '{name}'", file=sys.stderr)
        return 1
    live = running_pids(meta)
    if live:
        print(f"[ovid2 dev] stopping '{name}' (pids {live}) …")
        _stop_pids(live)
    if meta.get("port"):
        release_lease(int(meta["port"]))
    meta["pids"] = {}
    write_meta(root, meta)
    print(f"[ovid2 dev] '{name}' stopped")
    if args.purge:
        if not args.yes:
            answer = input(f"Delete {root} and all its state? [y/N] ").strip().lower()
            if answer not in {"y", "yes"}:
                print("[ovid2 dev] kept instance directory")
                return 0
        shutil.rmtree(root)
        print(f"[ovid2 dev] removed {root}")
    return 0


def _cmd_restart(args: argparse.Namespace) -> int:
    args.purge = False
    if read_meta(instance_root(validate_name(args.name))):
        _cmd_down(args)
    return _cmd_up(args)


def _cmd_ls(args: argparse.Namespace) -> int:
    if args.all:
        leases = list_leases()
        if not leases:
            print("[ovid2 dev] no dev instances on this host")
            return 0
        print(f"{'PORT':<6} {'MCP':<6} {'USER':<24} {'NAME':<16} STATUS")
        for lease in leases:
            status = "running" if lease.alive() else "stale"
            print(f"{lease.port:<6} {lease.mcp_port:<6} {lease.user:<24} {lease.name:<16} {status}")
        return 0
    found = False
    print(f"{'NAME':<16} {'STATUS':<8} {'URL':<28} ROOT")
    for name, root in _iter_instances():
        found = True
        meta = read_meta(root)
        live = running_pids(meta)
        status = "up" if "web" in live else ("partial" if live else "down")
        url = _url(meta) if live else "-"
        print(f"{name:<16} {status:<8} {url:<28} {root}")
    if not found:
        print("(none — create one with `ovid2 dev up`)")
    return 0


def _cmd_logs(args: argparse.Namespace) -> int:
    root = instance_root(validate_name(args.name))
    path = root / "logs" / f"{args.process}.log"
    if not path.exists():
        print(f"[ovid2 dev] no log at {path}", file=sys.stderr)
        return 1
    print(_tail(path, args.lines))
    if not args.follow:
        return 0
    with open(path, encoding="utf-8", errors="replace") as handle:
        handle.seek(0, os.SEEK_END)
        try:
            while True:
                chunk = handle.read()
                if chunk:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            return 0


def _cmd_env(args: argparse.Namespace) -> int:
    name = validate_name(args.name)
    root = instance_root(name)
    meta = read_meta(root)
    if not meta:
        print(f"[ovid2 dev] no instance named '{name}'; run `ovid2 dev up {name}`", file=sys.stderr)
        return 1
    env = instance_env(
        root,
        name=name,
        host=str(meta["host"]),
        port=int(meta["port"]),
        mcp_port=int(meta["mcp_port"]),
        image=str(meta["image"]),
        base={},
    )
    for key in _LEAKY_ENV:
        if key not in env:
            print(f"unset {key}")
    for key in sorted(env):
        print(f"export {key}={shlex.quote(env[key])}")
    return 0


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``dev`` subcommand group."""
    parser = subparsers.add_parser(
        "dev",
        help="Run isolated per-developer Ovid2 instances on a shared host.",
        description=(
            "Start/stop private Ovid2 instances (web + worker [+ mcp]) with their own "
            "ports, state tree, session cookie and Docker image tag."
        ),
    )
    action = parser.add_subparsers(dest="dev_action", metavar="ACTION", required=True)

    def _named(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("name", nargs="?", default="default", help="Instance name.")

    starters = (
        ("up", "Start (and on first use, create) an instance.", _cmd_up),
        ("restart", "Stop then start an instance.", _cmd_restart),
    )
    for verb, help_text, entry in starters:
        sub = action.add_parser(verb, help=help_text)
        _named(sub)
        sub.add_argument("--port", type=int, default=None, help="Exact web port (MCP = +1).")
        sub.add_argument("--host", default=None, help="Bind address (default: host LAN IP).")
        sub.add_argument("--image", default=None, help="Docker image tag for runs.")
        sub.add_argument("--no-worker", action="store_true", help="Do not start web-worker.")
        sub.add_argument("--mcp", action="store_true", help="Also start the MCP HTTP server.")
        sub.add_argument("--timeout", type=float, default=60.0, help="Startup wait (s).")
        sub.set_defaults(_command="dev", _entry=entry)

    down = action.add_parser("down", help="Stop an instance and release its ports.")
    _named(down)
    down.add_argument("--purge", action="store_true", help="Also delete its state tree.")
    down.add_argument("-y", "--yes", action="store_true", help="Do not ask before --purge.")
    down.set_defaults(_command="dev", _entry=_cmd_down)

    ls = action.add_parser("ls", help="List your instances (or everyone's with --all).")
    ls.add_argument("--all", action="store_true", help="Show every lease on this host.")
    ls.set_defaults(_command="dev", _entry=_cmd_ls)

    logs = action.add_parser("logs", help="Show an instance log.")
    _named(logs)
    logs.add_argument("process", nargs="?", default="web", choices=_PROCESSES)
    logs.add_argument("-f", "--follow", action="store_true", help="Follow the log.")
    logs.add_argument("-n", "--lines", type=int, default=50, help="Lines to show.")
    logs.set_defaults(_command="dev", _entry=_cmd_logs)

    env = action.add_parser("env", help="Print shell exports for an instance.")
    _named(env)
    env.set_defaults(_command="dev", _entry=_cmd_env)


if __name__ == "__main__":
    raise SystemExit("run via `ovid2 dev …`")
