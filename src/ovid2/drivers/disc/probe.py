"""Disc-driver probe helpers.

Disc has no SDK — auth / read / write probes are pure filesystem
checks. Kept in a dedicated module because the D3 recipe mandates the
four-file split (``metadata`` + ``driver`` + ``probe`` + ``spark``)
so contract tests can enforce import safety per file rather than per
method.
"""

from __future__ import annotations

import os

from ovid2.drivers.base import ProbeResult


def probe_auth_from_host_path(host_path: str, detail: dict[str, object]) -> ProbeResult:
    """Return an ``auth`` probe result for a disc host path.

    Args:
        host_path: Absolute directory on the host.
        detail: Extra data (host_path / container_path / mount_mode) to
            include on the result for diagnostic display.

    Returns:
        :class:`~ovid2.drivers.base.ProbeResult` with ``capability="auth"``.
    """
    if not host_path:
        return ProbeResult(
            ok=False,
            code="config",
            message="Connection has no host_path configured.",
            capability="auth",
        )
    if not os.path.isdir(host_path):
        return ProbeResult(
            ok=False,
            code="unreachable",
            message=f"Directory not found: {host_path}",
            capability="auth",
            detail=dict(detail),
        )
    if not os.access(host_path, os.R_OK):
        return ProbeResult(
            ok=False,
            code="auth",
            message=f"No read permission on {host_path}",
            capability="auth",
            detail=dict(detail),
        )
    return ProbeResult(
        ok=True,
        code="ok",
        message=f"Readable directory: {host_path}",
        capability="auth",
        detail=dict(detail),
    )


def probe_read_from_host_path(host_path: str, detail: dict[str, object]) -> ProbeResult:
    """Return a ``read`` probe result — verifies the host path is listable.

    A read probe is really just "auth + can-list-directory". Uses
    ``os.listdir`` under a small try/except so a directory that is
    readable-but-not-listable still surfaces cleanly (rare on posix,
    common on some NFS mounts).
    """
    auth = probe_auth_from_host_path(host_path, detail)
    if not auth.ok:
        return ProbeResult(
            ok=auth.ok, code=auth.code, message=auth.message,
            capability="read", detail=dict(auth.detail),
        )
    try:
        os.listdir(host_path)
    except OSError as exc:
        return ProbeResult(
            ok=False,
            code="auth",
            message=f"Cannot list {host_path}: {exc}",
            capability="read",
            detail=dict(detail),
        )
    return ProbeResult(
        ok=True,
        code="ok",
        message=f"Listable directory: {host_path}",
        capability="read",
        detail=dict(detail),
    )


def probe_write_from_host_path(host_path: str, detail: dict[str, object]) -> ProbeResult:
    """Return a ``write`` probe result — attempts a tiny file create + delete.

    Only runs when the mount_mode is ``rw``; otherwise returns a
    ``not_implemented``-style ``skipped`` result. On success creates
    and immediately removes a ``.ovid2_probe_<pid>`` file under
    ``host_path``.
    """
    mode = str(detail.get("mount_mode") or "").strip().lower()
    if mode == "ro":
        return ProbeResult(
            ok=True,
            code="skipped",
            message="mount_mode is 'ro'; skipping write probe.",
            capability="write",
            detail=dict(detail),
        )
    auth = probe_auth_from_host_path(host_path, detail)
    if not auth.ok:
        return ProbeResult(
            ok=auth.ok, code=auth.code, message=auth.message,
            capability="write", detail=dict(auth.detail),
        )
    probe_file = os.path.join(host_path, f".ovid2_probe_{os.getpid()}")
    try:
        with open(probe_file, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe_file)
    except OSError as exc:
        return ProbeResult(
            ok=False,
            code="auth",
            message=f"Cannot write to {host_path}: {exc}",
            capability="write",
            detail=dict(detail),
        )
    return ProbeResult(
        ok=True,
        code="ok",
        message=f"Writable directory: {host_path}",
        capability="write",
        detail=dict(detail),
    )


__all__ = [
    "probe_auth_from_host_path",
    "probe_read_from_host_path",
    "probe_write_from_host_path",
]
