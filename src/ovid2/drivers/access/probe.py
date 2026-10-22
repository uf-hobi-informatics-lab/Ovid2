"""Access-driver probe helpers.

Probes are pure filesystem checks — they verify the mounted Access
file exists and is readable. Deliberately free of pyspark/JDBC imports
so this module stays import-safe (only :mod:`ovid2.drivers.access.spark`
may touch the JVM). Table-level verification (opening the file over
JDBC) happens on the data plane, not here, because a probe must run in
the web tier without a Spark session.
"""

from __future__ import annotations

import os

from ovid2.drivers.access.metadata import ACCESS_FILE_SUFFIXES
from ovid2.drivers.base import ProbeResult


def probe_auth_from_host_path(
    host_path: str,
    detail: dict[str, object],
) -> ProbeResult:
    """Return an ``auth`` probe result for an Access connection directory.

    Args:
        host_path: Absolute directory on the host holding the file(s).
        detail: Extra data (host_path / container_path / mount_mode) to
            attach for diagnostic display.

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


def probe_read_from_file(
    file_path: str,
    detail: dict[str, object],
) -> ProbeResult:
    """Return a ``read`` probe result — verifies the Access file is readable.

    When ``file_path`` names a directory (no per-address file selected
    yet), the probe degrades to "directory contains at least one
    Access file". When it names a file, it must exist, be a regular
    file with a recognised suffix, and be readable.
    """
    if not file_path:
        return ProbeResult(
            ok=False,
            code="config",
            message="No file path resolved for the read probe.",
            capability="read",
            detail=dict(detail),
        )

    if os.path.isdir(file_path):
        try:
            children = os.listdir(file_path)
        except OSError as exc:
            return ProbeResult(
                ok=False,
                code="auth",
                message=f"Cannot list {file_path}: {exc}",
                capability="read",
                detail=dict(detail),
            )
        access_files = [c for c in children if c.lower().endswith(ACCESS_FILE_SUFFIXES)]
        if not access_files:
            return ProbeResult(
                ok=False,
                code="unreachable",
                message=f"No .accdb/.mdb files found in {file_path}",
                capability="read",
                detail=dict(detail),
            )
        return ProbeResult(
            ok=True,
            code="ok",
            message=(f"Found {len(access_files)} Access file(s) in {file_path}"),
            capability="read",
            detail=dict(detail),
        )

    if not os.path.isfile(file_path):
        return ProbeResult(
            ok=False,
            code="unreachable",
            message=f"Access file not found: {file_path}",
            capability="read",
            detail=dict(detail),
        )
    if not file_path.lower().endswith(ACCESS_FILE_SUFFIXES):
        return ProbeResult(
            ok=False,
            code="config",
            message=(f"Not an Access file (expected .accdb/.mdb): {file_path}"),
            capability="read",
            detail=dict(detail),
        )
    if not os.access(file_path, os.R_OK):
        return ProbeResult(
            ok=False,
            code="auth",
            message=f"No read permission on {file_path}",
            capability="read",
            detail=dict(detail),
        )
    return ProbeResult(
        ok=True,
        code="ok",
        message=f"Readable Access file: {file_path}",
        capability="read",
        detail=dict(detail),
    )


__all__ = [
    "probe_auth_from_host_path",
    "probe_read_from_file",
]
