"""Per-address disc bind-mount discovery.

Every ``disc`` connection referenced by an address contributes an
:class:`~ovid2.tools.extra_mounts.ExtraMount` to the per-invocation
Docker container. Without this, the pipeline's container-side
``mkdir`` walks up looking for a mount root and dies with
``PermissionError`` on ``/processing`` (or wherever it stops).

The helper is deliberately isolated from :mod:`ovid2.tools.extra_mounts`
(which owns the CLI-facing ``--extra-mount`` grammar) so the two
concerns stay separately testable:

- ``extra_mounts.parse_extra_mount``: validate a *user-supplied*
  string spec.
- ``disc_mounts.build_disc_mounts``: derive mounts from *declared
  connections*.

Both eventually produce :class:`ExtraMount` values that flow into the
same compose ``volumes:`` list, so the standard mount set stays a
single source of truth.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

from ovid2.tools.extra_mounts import (
    FORBIDDEN_CONTAINER_PATHS,
    RESERVED_CONTAINER_PATHS,
    ExtraMount,
)

logger = logging.getLogger(__name__)

__all__ = [
    "build_disc_mounts",
    "disc_mount_for_connection",
]


def _looks_like_disc(payload: dict[str, object]) -> bool:
    """Return True when a raw address JSON resolves to a disc connection."""
    if payload.get("_shape") == "connection":
        return _connection_type(str(payload.get("connection") or "")) == "disc"
    if payload.get("_shape") == "legacy":
        return str(payload.get("src_name") or "") == "disc"
    return False


def _connection_type(name: str) -> str | None:
    """Return the ``type`` field of a named connection, ``None`` on miss."""
    if not name:
        return None
    try:
        from ovid2.config import connections as _connections
        cfg = _connections.load_connection_config(name)
    except (FileNotFoundError, ValueError):
        return None
    return str(cfg.get("type") or "") or None


def disc_mount_for_connection(connection_name: str) -> ExtraMount | None:
    """Return the auto-generated :class:`ExtraMount` for one disc connection.

    Args:
        connection_name: Filename stem of a disc connection JSON.

    Returns:
        A validated :class:`ExtraMount`, or ``None`` when the
        connection is missing, non-disc, or produces a mount that
        would shadow a reserved container path.
    """
    from ovid2.config import connections as _connections
    from ovid2.drivers.disc import normalise_disc_args

    try:
        cfg = _connections.load_connection_config(connection_name)
    except (FileNotFoundError, ValueError):
        return None
    if str(cfg.get("type") or "") != "disc":
        return None
    args = normalise_disc_args(dict(cfg.get("args") or {}))
    host = args["host_path"]
    container = args["container_path"] or host
    mode = args["mount_mode"] or "rw"
    if not host or not os.path.isabs(host):
        logger.warning(
            "disc connection %r has no absolute host_path; skipping auto-mount.",
            connection_name,
        )
        return None
    if not os.path.isabs(container):
        logger.warning(
            "disc connection %r has non-absolute container_path %r; "
            "skipping auto-mount.",
            connection_name, container,
        )
        return None
    if container in RESERVED_CONTAINER_PATHS or container in FORBIDDEN_CONTAINER_PATHS:
        logger.warning(
            "disc connection %r targets reserved/forbidden container path %r; "
            "skipping auto-mount (the standard mount set already populates it).",
            connection_name, container,
        )
        return None
    if mode not in ("rw", "ro"):
        mode = "rw"
    if not os.path.exists(host):
        # Skip cleanly — the pipeline will produce a clearer error
        # once it actually tries to open the path. Refusing to launch
        # here would block legitimate cases (host_path exists on a
        # sibling host, disc lives on a temporarily-detached mount).
        logger.warning(
            "disc connection %r host_path %r does not exist on this host; "
            "skipping auto-mount.",
            connection_name, host,
        )
        return None
    return ExtraMount(host=host, container=container, mode=mode)


def build_disc_mounts(
    address_names: Iterable[str],
    *,
    explicit_container_paths: Iterable[str] = (),
) -> list[ExtraMount]:
    """Return a de-duplicated list of disc auto-mounts for the given addresses.

    Args:
        address_names: Address names referenced by the current run.
            Names that resolve to non-disc connections (or to missing
            addresses) are silently skipped.
        explicit_container_paths: Container paths already claimed by
            operator-supplied ``--extra-mount`` values. When an
            auto-mount would collide, the explicit mount wins and a
            warning names the connection whose auto-mount was dropped.

    Returns:
        A list of :class:`ExtraMount` values. Empty when no disc
        connection contributes a valid mount.
    """
    from ovid2.config.addresses import load_address_config

    explicit_set = {p.strip() for p in explicit_container_paths if p and p.strip()}
    seen: set[str] = set()
    mounts: list[ExtraMount] = []
    for address_name in address_names:
        if not address_name:
            continue
        try:
            payload = load_address_config(address_name)
        except (FileNotFoundError, ValueError):
            continue
        if not _looks_like_disc(payload):
            continue
        if payload.get("_shape") == "connection":
            connection_name = str(payload.get("connection") or "")
            mount = disc_mount_for_connection(connection_name)
        else:
            # Legacy addresses carry their disc path inline as
            # src_args.path. We synthesise an ExtraMount from that
            # path so pre-W7 configs still auto-mount.
            src_args = payload.get("src_args") or {}
            legacy_path = str(src_args.get("path") or "")
            mount = _mount_from_legacy_disc_path(address_name, legacy_path)
        if mount is None:
            continue
        if mount.container in explicit_set:
            logger.warning(
                "Explicit --extra-mount overrides auto-mount for address %r "
                "(container path %s).",
                address_name, mount.container,
            )
            continue
        if mount.container in seen:
            continue
        seen.add(mount.container)
        mounts.append(mount)
    return mounts


def _mount_from_legacy_disc_path(
    address_name: str, path: str
) -> ExtraMount | None:
    """Synthesise an auto-mount from a pre-W7 disc address path.

    Used for the last-mile of backward compatibility: an address that
    still uses ``{"src_name": "disc", "src_args": {"path": "/x"}}``
    contributes a rw auto-mount at ``/x:/x`` when ``/x`` exists on the
    host and does not shadow a reserved path. Skipped silently in
    every other case — legacy configs whose disc path already lives
    under ``/data`` do not need an extra mount and the standard
    mount set covers them.
    """
    if not path or not os.path.isabs(path):
        return None
    if path in RESERVED_CONTAINER_PATHS or path in FORBIDDEN_CONTAINER_PATHS:
        return None
    if not os.path.exists(path):
        logger.warning(
            "Legacy disc address %r references non-existent path %r; "
            "skipping auto-mount.",
            address_name, path,
        )
        return None
    return ExtraMount(host=path, container=path, mode="rw")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(
        "This module is imported by ovid2 CLI dispatchers; it is not a script."
    )
