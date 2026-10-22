"""Shared parser + validator for ``--extra-mount`` on ``ovid2 tools copy``.

A copy job's destination address may legitimately point at a host path
that isn't already bind-mounted into the ETL container (the standard
mount set is ``/app``, ``/data``, ``/ovid2-user-config``,
``/ovid_credentials``, ``/usr/include/rsa`` plus per-instance
``docker_data/spark_tmp``). ``--extra-mount`` lets the operator add
per-invocation bind mounts to that container. Only ``ovid2 tools
copy`` accepts the flag; the ETL family (etl / analysis / dq_checks)
is confined to the standard mount set on purpose.

Wire format: ``HOST:CONTAINER[:MODE]`` (mirrors ``docker run -v``).

- ``HOST`` — absolute path on the host that already exists.
- ``CONTAINER`` — absolute path inside the container. May not shadow
  any of the standard mounts (``/app``, ``/data``,
  ``/ovid2-user-config``, ``/ovid_credentials``,
  ``/usr/include/rsa``), nor sit under ``/tmp`` or the container
  root ``/``. This prevents an accidental override of the pipeline
  code, config tree, or credential material.
- ``MODE`` — ``rw`` (default) or ``ro``. Copy destinations almost
  always want ``rw``; ``ro`` is accepted for the rare case of
  reading from an extra host-only path.

Validation runs at the copy CLI's argparse-post-parse hook so the
same rules apply whether the caller is the web worker's
``subprocess.Popen``, an interactive shell, an MCP tool, or a future
scheduler.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ExtraMountError(ValueError):
    """Raised when an ``--extra-mount`` spec is malformed or unsafe.

    Subclasses :class:`ValueError` so argparse ``type=`` callables can
    let the exception propagate; argparse converts it into a clean
    ``--extra-mount: <message>`` error.
    """


#: Container paths that are always populated by the standard bind-mount
#: set. Shadowing any of these with an ``--extra-mount`` would silently
#: replace the pipeline source tree, config tree, credential material,
#: or input data — all footguns big enough to justify a hard reject.
RESERVED_CONTAINER_PATHS: frozenset[str] = frozenset({
    "/app",
    "/data",
    "/ovid2-user-config",
    "/ovid_credentials",
    "/usr/include/rsa",
})

#: Container paths that are illegal on their own (``/`` shadows the
#: entire container fs; ``/tmp`` is a Bitnami-managed writable tmpfs
#: whose contents the pipeline relies on).
FORBIDDEN_CONTAINER_PATHS: frozenset[str] = frozenset({"/", "/tmp"})


@dataclass(frozen=True)
class ExtraMount:
    """A validated ``host:container[:mode]`` mount.

    Attributes:
        host: Absolute host path (verified to exist at parse time).
        container: Absolute container path.
        mode: Either ``rw`` or ``ro``.
    """

    host: str
    container: str
    mode: str

    def as_docker_v(self) -> str:
        """Return the ``-v`` argument value for ``docker run``."""
        return f"{self.host}:{self.container}:{self.mode}"

    def as_compose_volume(self) -> str:
        """Return the ``volumes:`` list entry for a compose service."""
        return f"{self.host}:{self.container}:{self.mode}"


def parse_extra_mount(spec: str) -> ExtraMount:
    """Parse a single ``host:container[:mode]`` spec.

    Args:
        spec: Raw string from ``--extra-mount`` / the web form / MCP.

    Returns:
        The validated :class:`ExtraMount`.

    Raises:
        ExtraMountError: If the spec is malformed, references a
            non-existent host path, uses a relative path, targets a
            reserved container path, or specifies an unknown mode.
    """
    if not spec or not isinstance(spec, str):
        raise ExtraMountError("extra-mount spec must be a non-empty string")

    parts = spec.split(":")
    if len(parts) == 2:
        host, container = parts
        mode = "rw"
    elif len(parts) == 3:
        host, container, mode = parts
    else:
        raise ExtraMountError(
            f"extra-mount must be HOST:CONTAINER[:MODE], got {spec!r}"
        )

    host = host.strip()
    container = container.strip()
    mode = mode.strip().lower()

    if not host:
        raise ExtraMountError(f"extra-mount host path is empty: {spec!r}")
    if not container:
        raise ExtraMountError(f"extra-mount container path is empty: {spec!r}")
    if not os.path.isabs(host):
        raise ExtraMountError(
            f"extra-mount host path must be absolute, got {host!r}"
        )
    if not os.path.isabs(container):
        raise ExtraMountError(
            f"extra-mount container path must be absolute, got {container!r}"
        )
    # Reject relative-segment shenanigans (``..`` or ``.``). ``normpath``
    # resolves these; if the result differs, the caller was being cute
    # in a way we don't want to compile into a ``docker run`` argv.
    if os.path.normpath(host) != host:
        raise ExtraMountError(
            f"extra-mount host path must be normalized, got {host!r}"
        )
    if os.path.normpath(container) != container:
        raise ExtraMountError(
            f"extra-mount container path must be normalized, got {container!r}"
        )
    if container in RESERVED_CONTAINER_PATHS:
        raise ExtraMountError(
            f"extra-mount container path {container!r} is reserved; the "
            "standard mount set already populates it — pick a different "
            "container path"
        )
    if container in FORBIDDEN_CONTAINER_PATHS:
        raise ExtraMountError(
            f"extra-mount container path {container!r} is forbidden"
        )
    if mode not in ("rw", "ro"):
        raise ExtraMountError(
            f"extra-mount mode must be 'rw' or 'ro', got {mode!r}"
        )
    if not os.path.exists(host):
        raise ExtraMountError(
            f"extra-mount host path does not exist: {host!r}"
        )

    return ExtraMount(host=host, container=container, mode=mode)


def parse_extra_mounts(specs: list[str] | None) -> list[ExtraMount]:
    """Parse a list of extra-mount specs.

    Args:
        specs: List of raw strings, or ``None``.

    Returns:
        A list of validated :class:`ExtraMount` objects. Empty list
        when ``specs`` is falsy.

    Raises:
        ExtraMountError: On the first malformed / unsafe entry. The
            error message names the offending index so a form user
            can pin down the bad line.

        ExtraMountError: If two entries claim the same container
            path (docker would accept it and last-wins, which is
            surprising; explicit rejection is safer).
    """
    if not specs:
        return []

    parsed: list[ExtraMount] = []
    seen_containers: set[str] = set()
    for i, raw in enumerate(specs):
        try:
            m = parse_extra_mount(raw)
        except ExtraMountError as exc:
            raise ExtraMountError(f"extra-mount #{i + 1}: {exc}") from None
        if m.container in seen_containers:
            raise ExtraMountError(
                f"extra-mount #{i + 1}: container path {m.container!r} "
                "is already mounted by an earlier --extra-mount"
            )
        seen_containers.add(m.container)
        parsed.append(m)
    return parsed
