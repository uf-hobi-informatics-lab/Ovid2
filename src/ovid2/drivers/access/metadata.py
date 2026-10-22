"""Import-safe metadata for the ``access`` driver.

Everything the web tier and MCP need to describe the Microsoft Access
peripheral — connection/location form fields, container-requirement
derivation, and arg-shape widening. Deliberately free of pyspark and
JDBC imports so :mod:`ovid2.drivers.access` can be enumerated by the
web control panel without loading Spark. Enforced by
``tests/contract/test_driver_metadata_import_safe.py``.

An ``access`` *connection* points at a directory on the host that
holds one or more ``.accdb`` / ``.mdb`` files (widened to the disc
``host_path`` / ``container_path`` / ``mount_mode`` triple so the
existing bind-mount collector can mount it read-only). An ``access``
*location* selects one database file under that directory via ``path``
and, optionally, narrows the set of tables materialised via ``tables``
/ ``exclude_tables``.
"""

from __future__ import annotations

from typing import Any

from ovid2.drivers.base import ConnectionFieldSpec, FieldSpec, LocationField
from ovid2.drivers.container import ContainerRequirement, ExtraMount

#: Valid ``mount_mode`` values. An Access source is read-only by
#: default (``ro``) — the pipeline only ever reads from the ``.accdb``
#: file, never writes back to it.
_VALID_MOUNT_MODES: frozenset[str] = frozenset({"rw", "ro"})

#: Recognised Access database file extensions.
ACCESS_FILE_SUFFIXES: tuple[str, ...] = (".accdb", ".mdb")


def normalise_access_args(args: dict[str, Any]) -> dict[str, Any]:
    """Return a dict with ``host_path`` / ``container_path`` / ``mount_mode``.

    Mirrors :func:`ovid2.drivers.disc.metadata.normalise_disc_args` so
    the Access source reuses the exact bind-mount machinery. Tolerates
    three input shapes:

    1. Legacy: ``{"root_path": "/x"}`` → widened to
       ``host_path == container_path == root_path``, ``mount_mode == "ro"``.
    2. New: ``{"host_path": "/x", "container_path": "/y", "mount_mode": "ro"}``.
       ``container_path`` defaults to ``host_path`` when absent.
    3. Mixed: both present — ``host_path`` wins.

    The default ``mount_mode`` is ``ro`` (unlike disc, whose default is
    ``rw``) because an Access connection is a source feed that must not
    be mutated.
    """
    host = str(args.get("host_path") or args.get("root_path") or "").strip()
    container = str(args.get("container_path") or "").strip() or host
    mount_mode = str(args.get("mount_mode") or "ro").strip().lower()
    if mount_mode not in _VALID_MOUNT_MODES:
        mount_mode = "ro"
    return {
        "host_path": host,
        "container_path": container,
        "mount_mode": mount_mode,
    }


def connection_fields() -> ConnectionFieldSpec:
    """Return the connection-form field spec for an Access connection."""
    return ConnectionFieldSpec(
        args=(
            FieldSpec(
                name="host_path",
                required=True,
                widget="path_picker",
                help=(
                    "Absolute path on the host to the directory holding "
                    "the Access database file(s). Use the Browse button "
                    "to pick it. This directory is bind-mounted (read-only "
                    "by default) into the ETL container."
                ),
            ),
            FieldSpec(
                name="container_path",
                required=False,
                help=(
                    "Path inside the container. Defaults to host_path — "
                    "keep them equal unless the container should see a "
                    "different path."
                ),
            ),
            FieldSpec(
                name="mount_mode",
                required=False,
                default="ro",
                help=(
                    "Bind-mount mode. Defaults to 'ro' — an Access source "
                    "is read-only. Set 'rw' only if you have a specific "
                    "reason to allow writes to the source directory."
                ),
            ),
        ),
        secrets=(),
        note=(
            "Access connections read Microsoft Access (.accdb / .mdb) "
            "database files via the UCanAccess JDBC driver baked into the "
            "ETL image. Each user table inside a file is surfaced as a "
            "separate table; system tables (MSys*, HSQLDB internals) are "
            "hidden. The connection directory is auto-mounted read-only."
        ),
    )


def location_fields() -> tuple[LocationField, ...]:
    """Return the location-form field spec for an Access address."""
    return (
        LocationField(
            name="path",
            required=True,
            widget="str",
            help=(
                "Path to the .accdb / .mdb file, relative to the "
                "connection's host_path. E.g. 'drug_reference v2026.accdb'."
            ),
        ),
        LocationField(
            name="tables",
            required=False,
            widget="str",
            help=(
                "Optional comma-separated allow-list of table names to "
                "read. Leave blank to read every user table in the file "
                "(each becomes a separate output table)."
            ),
        ),
        LocationField(
            name="exclude_tables",
            required=False,
            widget="str",
            help=(
                "Optional comma-separated list of table names to skip. "
                "Applied after 'tables'. System tables are always skipped."
            ),
        ),
        LocationField(
            name="lowercase_columns",
            required=False,
            widget="bool",
            help="Lowercase every column name on read.",
        ),
    )


def parse_table_list(raw: Any) -> tuple[str, ...]:
    """Parse a table allow/deny list from a location value.

    Accepts either a comma-separated string (``"A, B ,C"``) or an
    already-split list/tuple. Returns a tuple of stripped, non-empty
    names preserving order and de-duplicating case-insensitively.
    """
    if raw is None:
        return ()
    items = [str(x) for x in raw] if isinstance(raw, (list, tuple)) else str(raw).split(",")
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        name = item.strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(name)
    return tuple(result)


def container_requirements_from(
    args: dict[str, Any],
    location: dict[str, Any] | None = None,
) -> tuple[ContainerRequirement, ...]:
    """Return the auto-mount for an Access peripheral, given its args.

    Kept as a module-level function so the driver-based collector's
    legacy path (which builds a driver instance from a raw ``src_args``
    dict) can call it without going through the class. Reserved /
    forbidden container-path filtering is NOT done here — that policy
    lives on :mod:`ovid2.drivers.collector`.

    Recognises the same three arg shapes as disc:

    - W7 connection args: ``{host_path, container_path?, mount_mode?}``.
    - Legacy ``root_path``: widened via :func:`normalise_access_args`.
    - Pre-W7 address (``src_name: access``): ``{path: '/dir/file.accdb'}``
      inside ``src_args`` — the directory holding the file is mounted.
    """
    del location  # the mount is the connection directory, not per-file
    # Legacy-address shape: ``src_args.path`` carries an absolute path
    # (possibly to the file itself). Mount its parent directory when it
    # looks like a file, else mount the path verbatim.
    if "host_path" not in args and "root_path" not in args and "path" in args:
        raw = str(args.get("path") or "").strip()
        if not raw:
            return ()
        host = _directory_of(raw)
        return (ExtraMount(host_path=host, container_path=host, mode="ro"),)

    normalised = normalise_access_args(args)
    host = normalised["host_path"]
    container = normalised["container_path"] or host
    mode = normalised["mount_mode"] or "ro"
    if not host or not container:
        return ()
    return (ExtraMount(host_path=host, container_path=container, mode=mode),)


def _directory_of(path: str) -> str:
    """Return the directory portion of ``path`` if it names a file.

    Uses only string inspection (no filesystem access, so it stays
    import-safe and deterministic): if the final path segment ends in a
    recognised Access suffix, the parent directory is returned;
    otherwise the path is assumed to already be a directory and is
    returned unchanged.
    """
    import posixpath

    lowered = path.lower()
    if lowered.endswith(ACCESS_FILE_SUFFIXES):
        parent = posixpath.dirname(path.rstrip("/"))
        return parent or path
    return path


__all__ = [
    "ACCESS_FILE_SUFFIXES",
    "connection_fields",
    "container_requirements_from",
    "location_fields",
    "normalise_access_args",
    "parse_table_list",
]
