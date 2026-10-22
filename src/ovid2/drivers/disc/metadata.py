"""Import-safe metadata for the ``disc`` driver.

Everything the web tier and MCP need to describe the disc peripheral —
form fields, location fields, container-requirement derivation,
legacy-arg shape widening. Deliberately free of pyspark and vendor
SDK imports so :mod:`ovid2.drivers.disc` can be enumerated by the web
control panel without loading Spark.

Contains:

- :data:`_VALID_MOUNT_MODES` — the ``rw`` / ``ro`` allow-list.
- :func:`normalise_disc_args` — legacy ``{root_path}`` → new
  ``{host_path, container_path, mount_mode}`` widener. Public because
  the web (:mod:`ovid2.web.services.connections_svc`,
  :mod:`ovid2.web.services.fs_svc`) reads legacy JSONs and needs the
  same shape.
- :func:`connection_fields` / :func:`location_fields` — the field-spec
  catalogue the D2-era :data:`_FIELD_SPECS` / :data:`_LOCATION_SPECS`
  used to publish. Returned as typed
  :class:`~ovid2.drivers.base.ConnectionFieldSpec` and
  :class:`~ovid2.drivers.base.LocationField` tuples.
- :func:`container_requirements_from` — the pure-function form of
  :meth:`DiscDriver.container_requirements_for`. Reused by the D5
  collector's legacy shape path so the requirement derivation is a
  single source of truth.
"""

from __future__ import annotations

from typing import Any

from ovid2.drivers.base import ConnectionFieldSpec, FieldSpec, LocationField
from ovid2.drivers.container import ContainerRequirement, ExtraMount

#: Valid ``mount_mode`` values. ``rw`` is the default because a disc
#: connection typically owns an output tree; feeds that must remain
#: untouched should be pinned to ``ro`` explicitly.
_VALID_MOUNT_MODES: frozenset[str] = frozenset({"rw", "ro"})


def normalise_disc_args(args: dict[str, Any]) -> dict[str, Any]:
    """Return a dict with ``host_path`` / ``container_path`` / ``mount_mode``.

    The loader tolerates three input shapes:

    1. Legacy: ``{"root_path": "/x"}`` (pre-mount phase). Widened to
       ``host_path == container_path == root_path``, ``mount_mode == "rw"``.
    2. New: ``{"host_path": "/x", "container_path": "/y", "mount_mode": "ro"}``.
       ``container_path`` defaults to ``host_path`` when absent.
    3. Mixed: both ``root_path`` and ``host_path`` present. ``host_path``
       wins; ``root_path`` is ignored.

    The returned dict always contains all three keys — every consumer
    (web display, pipeline resolver, mount builder) can rely on the
    same shape without re-checking for absent legacy fields.
    """
    host = str(args.get("host_path") or args.get("root_path") or "").strip()
    container = str(args.get("container_path") or "").strip() or host
    mount_mode = str(args.get("mount_mode") or "rw").strip().lower()
    if mount_mode not in _VALID_MOUNT_MODES:
        mount_mode = "rw"
    return {
        "host_path": host,
        "container_path": container,
        "mount_mode": mount_mode,
    }


def connection_fields() -> ConnectionFieldSpec:
    """Return the connection-form field spec for a disc connection.

    Mirrors the entry that lived at
    :data:`ovid2.connections.registry._FIELD_SPECS["disc"]` before
    D3.disc; kept byte-identical so the D0 catalogue-sync tests keep
    passing.
    """
    return ConnectionFieldSpec(
        args=(
            FieldSpec(
                name="host_path",
                required=True,
                widget="path_picker",
                help=(
                    "Absolute path on the host. Use the Browse button to "
                    "pick a directory. This is what gets bind-mounted into "
                    "the ETL container."
                ),
            ),
            FieldSpec(
                name="container_path",
                required=False,
                help=(
                    "Path inside the container. Defaults to host_path — "
                    "the pipeline and the picker share the same string, "
                    "which matches operator muscle memory. Set this only "
                    "when the container should see a different path."
                ),
            ),
            FieldSpec(
                name="mount_mode",
                required=False,
                default="rw",
                help=(
                    "Bind-mount mode applied to the auto-generated mount. "
                    "Use 'rw' for outputs and 'ro' for read-only feeds."
                ),
            ),
        ),
        secrets=(),
        note=(
            "Disc connections auto-generate a Docker bind mount so the "
            "pipeline can read/write outside the standard mount set "
            "(/data, /app, /ovid_credentials, /usr/include/rsa). Legacy "
            "root_path files still load and are surfaced as host_path."
        ),
    )


def location_fields() -> tuple[LocationField, ...]:
    """Return the location-form field spec for a disc address.

    Mirrors the entry that lived at
    :data:`ovid2.connections.registry._LOCATION_SPECS["disc"]` before
    D3.disc.
    """
    return (
        LocationField(
            name="path",
            required=False,
            widget="str",
            help=(
                "Sub-path relative to the connection's root_path. "
                "Leave blank to use the root itself."
            ),
        ),
        LocationField(
            name="format",
            required=False,
            widget="str",
            default="parquet",
            help="File format for reads/writes (parquet, csv, json, …).",
        ),
        LocationField(
            name="lowercase_columns",
            required=False,
            widget="bool",
            help="Lowercase every column name on read.",
        ),
        LocationField(
            name="single_file",
            required=False,
            widget="bool",
            default=True,
            help=(
                "CSV only: when true (the default for disc), merge the "
                "Spark part-files into a single ``{table}.csv`` on write. "
                "Set to false to keep the raw part-directory layout."
            ),
        ),
    )


def container_requirements_from(
    args: dict[str, Any],
    location: dict[str, Any] | None = None,
) -> tuple[ContainerRequirement, ...]:
    """Return the auto-mount for a disc peripheral, given its args.

    Kept as a module-level function so the D5 collector's legacy path
    (which builds a driver instance from a raw ``src_args`` dict) can
    call it without going through the class. Reserved / forbidden
    container-path filtering is NOT done here — that policy lives on
    :mod:`ovid2.drivers.collector` so it applies uniformly to every
    peripheral type.

    Recognises three arg shapes so both W7 and pre-W7 addresses
    produce identical mounts:

    - W7 disc connection args: ``{host_path, container_path?, mount_mode?}``.
    - Legacy ``root_path``: widened via :func:`normalise_disc_args`.
    - Pre-W7 disc address (``src_name: disc``): ``{path: '/x', …}``
      inside ``src_args``. Rewritten as
      ``host_path = container_path = path`` since the pipeline always
      saw both sides at the same absolute path.
    """
    # Legacy-address shape: ``src_args.path`` carries the host+container
    # path in one field.
    if "host_path" not in args and "root_path" not in args and "path" in args:
        host = str(args.get("path") or "").strip()
        if not host:
            return ()
        return (ExtraMount(host_path=host, container_path=host, mode="rw"),)

    normalised = normalise_disc_args(args)
    host = normalised["host_path"]
    container = normalised["container_path"] or host
    mode = normalised["mount_mode"] or "rw"
    if not host or not container:
        return ()
    return (ExtraMount(host_path=host, container_path=container, mode=mode),)


__all__ = [
    "connection_fields",
    "container_requirements_from",
    "location_fields",
    "normalise_disc_args",
]
