"""Least-privilege environments for executor subprocesses.

Every executor invocation the web tier or worker launches (``ovid2 tools
…`` broker calls, ``ovid2 etl`` / ``copy`` / ``move`` / ``health`` /
``export`` jobs, engine probes) gets:

1. An **allow-listed environment** (:func:`build_minimal_env`). Ambient
   credentials in the parent's environment — and the parent's own
   control-plane tokens (:data:`ALWAYS_DROP`) — never reach the child.
2. A **scoped secrets directory**: a private, per-invocation ``0o700``
   directory holding ``0o600`` copies of *only* the ``secrets.d`` files
   the invocation was granted (:class:`~ovid2.config.secret_refs.SecretScope`),
   exported as ``OVID2_SECRETS_DIR``. :func:`ovid2.config.secrets.load_secret`
   consults it first.

Roll-out mode (``OVID2_SECRETS_SCOPE_MODE``):

- ``warn`` (default): a secret outside the scope still resolves from the
  normal search path, with a warning naming it — so under-declared
  scopes surface in logs without breaking runs.
- ``enforce``: the scoped directory is the only place searched; a miss
  is a hard "secret not found".

Threat model — be precise about what this buys:

- **Host subprocesses** run as the same uid as the web tier, so a
  *compromised* host subprocess could still open ``~/.config/ovid2/secrets.d``
  directly. Host scoping is least authority *by construction* (correct
  code can only dereference what it was granted, and leaked tokens are
  gone from its env) — not a sandbox.
- **Containers** get real isolation: the docker launcher shadows the
  config tree's ``secrets.d`` with an empty tmpfs under ``enforce`` and
  bind-mounts back only the granted files
  (:func:`ovid2.core.docker._scoped_secret_mounts`).

Web-safe: no pyspark / driver / secret-*value* imports. Secret files are
copied byte-for-byte; their contents are never parsed here.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ovid2.config import paths as _paths
from ovid2.config.secret_refs import SecretScope, resolve_scope

__all__ = [
    "ALWAYS_DROP",
    "SCOPE_DIR_PREFIX",
    "ScopedSecrets",
    "build_minimal_env",
    "env_key_allowed",
    "prepare_scoped_secrets",
    "scoped_executor_env",
    "secret_source_dirs",
    "sweep_stale_scopes",
]

logger = logging.getLogger(__name__)

#: Variables removed from every executor env even when the allow-list
#: would keep them (``OVID2_*`` is a prefix entry):
#:
#: - ``OVID2_SECRETS_FILE`` answers *every* secret name — a scope bypass.
#: - ``OVID2_SECRETS_DIR`` from the parent is replaced by the child's own.
#: - The MCP / assistant API tokens are control-plane credentials the
#:   executor never needs.
ALWAYS_DROP: frozenset[str] = frozenset(
    {
        "OVID2_SECRETS_FILE",
        "OVID2_SECRETS_DIR",
        "OVID2_MCP_API_TOKEN",
        "OVID2_LLM_API_TOKEN",
    }
)

#: Directory-name prefix for per-invocation scopes (used by the sweeper).
SCOPE_DIR_PREFIX: str = "secrets-scope-"

_SCOPE_ENV = "OVID2_SECRETS_DIR"


def env_key_allowed(key: str, allowlist: tuple[str, ...]) -> bool:
    """Return ``True`` if ``key`` matches ``allowlist`` (``*`` suffix = prefix)."""
    for pattern in allowlist:
        if pattern.endswith("*"):
            if key.startswith(pattern[:-1]):
                return True
        elif key == pattern:
            return True
    return False


def build_minimal_env(source_env: Mapping[str, str], allowlist: tuple[str, ...]) -> dict[str, str]:
    """Return an allow-listed copy of ``source_env`` minus :data:`ALWAYS_DROP`."""
    return {
        k: v
        for k, v in source_env.items()
        if env_key_allowed(k, allowlist) and k not in ALWAYS_DROP
    }


def secret_source_dirs(env: Mapping[str, str] | None = None) -> list[Path]:
    """Return the unscoped ``secrets.d`` directories, in lookup order."""
    source = env if env is not None else os.environ
    xdg = source.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return [Path(xdg) / "ovid2" / "secrets.d", Path("/etc/ovid2/secrets.d")]


def _scope_root() -> Path:
    root = _paths.runtime_dir() / "exec"
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        return root
    except OSError:
        return Path(tempfile.gettempdir())


@dataclass
class ScopedSecrets:
    """A materialised per-invocation secrets directory.

    Attributes:
        path: The ``0o700`` directory exported as ``OVID2_SECRETS_DIR``.
        granted: File names copied in.
        missing: File names the scope named but no source had.
    """

    path: Path
    granted: tuple[str, ...]
    missing: tuple[str, ...]

    def cleanup(self) -> None:
        """Remove the directory. Idempotent."""
        shutil.rmtree(self.path, ignore_errors=True)


def prepare_scoped_secrets(
    scope: SecretScope,
    *,
    env: Mapping[str, str] | None = None,
    label: str = "exec",
) -> ScopedSecrets:
    """Create the scoped directory for ``scope`` and copy granted files in.

    Args:
        scope: What the invocation may dereference.
        env: Environment used to locate the source ``secrets.d``
            (defaults to ``os.environ``).
        label: Short tag embedded in the directory name (debugging aid).

    Returns:
        A :class:`ScopedSecrets` the caller must :meth:`~ScopedSecrets.cleanup`.
    """
    safe_label = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:40]
    path = Path(tempfile.mkdtemp(prefix=f"{SCOPE_DIR_PREFIX}{safe_label}-", dir=_scope_root()))
    path.chmod(0o700)
    sources = secret_source_dirs(env)
    granted: list[str] = []
    missing: list[str] = []
    try:
        wanted = sorted(resolve_scope(scope))
    except Exception:
        logger.warning("Could not resolve secret scope %r; granting no secrets.", scope)
        wanted = []
    for file_name in wanted:
        if "/" in file_name or file_name.startswith("."):
            missing.append(file_name)
            continue
        src = next((d / file_name for d in sources if (d / file_name).is_file()), None)
        if src is None:
            missing.append(file_name)
            continue
        dst = path / file_name
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out, src.open("rb") as inp:
            shutil.copyfileobj(inp, out)
        granted.append(file_name)
    if missing:
        logger.debug("Scoped secrets not found for %s: %s", label, missing)
    return ScopedSecrets(path=path, granted=tuple(granted), missing=tuple(missing))


@contextmanager
def scoped_executor_env(
    scope: SecretScope,
    *,
    allowlist: tuple[str, ...],
    base_env: Mapping[str, str] | None = None,
    label: str = "exec",
) -> Iterator[dict[str, str]]:
    """Yield a least-privilege env for one short-lived executor subprocess.

    The scoped directory is removed when the block exits (including on
    exception). Long-lived children (worker jobs) should call
    :func:`prepare_scoped_secrets` directly and clean up when the child
    is reaped.
    """
    source = dict(base_env if base_env is not None else os.environ)
    scoped = prepare_scoped_secrets(scope, env=source, label=label)
    try:
        env = build_minimal_env(source, allowlist)
        env[_SCOPE_ENV] = str(scoped.path)
        yield env
    finally:
        scoped.cleanup()


def sweep_stale_scopes(max_age_seconds: float = 24 * 3600) -> int:
    """Remove scoped directories left behind by a hard-killed parent.

    Returns:
        The number of directories removed.
    """
    root = _scope_root()
    now = time.time()
    removed = 0
    for entry in root.glob(f"{SCOPE_DIR_PREFIX}*"):
        try:
            if entry.is_dir() and now - entry.stat().st_mtime > max_age_seconds:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Library module; import ovid2.config.executor_env.")
