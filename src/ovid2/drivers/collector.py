"""Driver-based container-requirement collector.

Walks a list of address names, resolves each to its :class:`Driver`
via the D2 registry, and aggregates every driver's
:meth:`Driver.container_requirements_for` output into a typed
:class:`~ovid2.drivers.container.MergedRequirements` payload.

Superset of :func:`ovid2.connections.disc_mounts.build_disc_mounts`:

- Handles every driver type, not just disc. The disc-mount logic
  lives on the disc driver's ``container_requirements_for`` today
  (D2 :class:`LegacyDriverAdapter` bridging); native driver ports
  (D3.<type>) will move their per-type container needs (Java jars
  for s3 / azure_blob, env overlays for snowflake) here without any
  caller-side change.
- Returns a typed :class:`MergedRequirements` instead of a bare
  ``list[ExtraMount]`` — callers can consume mounts, java packages,
  and env overlays without additional plumbing.
- Reserved-path filtering (``/data``, ``/app``, ``/ovid_credentials``,
  ``/usr/include/rsa``) happens here rather than in each driver, so
  a new driver can declare a mount at ``/data`` in metadata and the
  collector will refuse it uniformly.

The collector is deliberately import-cheap: it never touches
pyspark / boto / azure at enumeration time. Only address JSONs on
disk plus the driver metadata are consulted.
"""

from __future__ import annotations

import logging
from typing import Iterable

from ovid2.drivers.container import (
    ContainerRequirement,
    EnvOverlay,
    ExtraMount,
    JavaPackage,
    MergedRequirements,
    collect_requirements,
)

logger = logging.getLogger(__name__)

#: Container paths owned by the standard mount set. A driver-declared
#: mount targeting one of these is silently dropped — the standard
#: mount already populates it, and letting the driver overwrite it
#: would break the container-uid / secrets pipeline.
RESERVED_CONTAINER_PATHS: frozenset[str] = frozenset(
    {"/data", "/app", "/ovid_credentials", "/usr/include/rsa"},
)

#: Paths a driver must never bind-mount to under any circumstance.
FORBIDDEN_CONTAINER_PATHS: frozenset[str] = frozenset(
    {"/", "/etc", "/usr", "/var", "/proc", "/sys", "/dev"},
)


def collect_for_addresses(
    address_names: Iterable[str],
    *,
    explicit_container_paths: Iterable[str] = (),
) -> MergedRequirements:
    """Return the merged container requirements for a set of addresses.

    Args:
        address_names: Address names referenced by the current run.
            Missing addresses or unregistered driver types are skipped
            with a warning; the collector never propagates.
        explicit_container_paths: Container paths already claimed by
            operator-supplied ``--extra-mount`` values. Auto-mounts
            targeting these paths are silently dropped so operator
            overrides always win.

    Returns:
        Typed :class:`MergedRequirements` — mounts, java packages,
        and env overlays each in their own bucket. De-duplicated by
        value.
    """
    explicit_set = {p.strip() for p in explicit_container_paths if p and p.strip()}
    seen_mount_containers: set[str] = set()
    reqs: list[ContainerRequirement] = []

    for address_name in address_names:
        if not address_name:
            continue
        driver_reqs = _requirements_for_address(address_name)
        for req in driver_reqs:
            if isinstance(req, ExtraMount):
                target = req.container_path
                if not target or not target.startswith("/"):
                    continue
                if target in FORBIDDEN_CONTAINER_PATHS:
                    logger.warning(
                        "Driver-declared mount on address %r targets "
                        "forbidden container path %r; dropping.",
                        address_name, target,
                    )
                    continue
                if target in RESERVED_CONTAINER_PATHS:
                    logger.warning(
                        "Driver-declared mount on address %r targets "
                        "reserved container path %r; the standard mount set "
                        "already populates it. Dropping auto-mount.",
                        address_name, target,
                    )
                    continue
                if target in explicit_set:
                    logger.warning(
                        "Explicit --extra-mount overrides auto-mount for "
                        "address %r (container path %s).",
                        address_name, target,
                    )
                    continue
                if target in seen_mount_containers:
                    continue
                seen_mount_containers.add(target)
                reqs.append(req)
            elif isinstance(req, (JavaPackage, EnvOverlay)):
                reqs.append(req)

    return collect_requirements(tuple(reqs))


def secret_refs_for_addresses(address_names: Iterable[str]) -> set[str]:
    """Return the ``secret_ref`` stems the given addresses depend on.

    Walks each address to its backing connection and collects the
    connection's ``secret_ref`` (the stem of a ``secrets.d/<ref>.json``
    file). No secret *values* are read — only the reference names — so
    this is safe to call from the (secret-free) enumeration path.

    Used by the executor least-privilege mount logic to bind-mount only
    the secret files a run actually needs, rather than the whole
    ``secrets.d/`` tree. Missing addresses / connections are skipped
    silently: an unresolvable reference simply contributes no secret.

    Args:
        address_names: Address names referenced by the current run.

    Returns:
        The set of secret-ref stems (without ``.json``).
    """
    from ovid2.config.addresses import load_address_config
    from ovid2.config.connections import load_connection_config

    refs: set[str] = set()
    for address_name in address_names:
        if not address_name:
            continue
        try:
            payload = load_address_config(address_name)
        except (FileNotFoundError, ValueError) as exc:
            logger.debug(
                "secret_refs_for_addresses skipping %r: %s", address_name, exc,
            )
            continue

        if payload.get("_shape") == "connection":
            connection_name = str(payload.get("connection") or "")
        else:
            # Legacy shape: ``src_name`` may name a connection whose
            # secret_ref we still want to honour.
            connection_name = str(payload.get("src_name") or "")

        if not connection_name:
            continue
        try:
            conn_raw = load_connection_config(connection_name)
        except (FileNotFoundError, ValueError):
            continue
        ref = conn_raw.get("secret_ref")
        if ref:
            refs.add(str(ref))
    return refs


def _requirements_for_address(
    address_name: str,
) -> tuple[ContainerRequirement, ...]:
    """Return the container requirements a single address contributes.

    Handles both address shapes:

    - **W7** (``{connection, location}``): resolves through the
      driver registry using the connection's type and the address's
      location payload.
    - **Legacy** (``{src_name, src_args}``): builds a driver
      directly from the legacy args. Only disc addresses actually
      contribute mounts today, but the mechanism is uniform.
    """
    from ovid2.config.addresses import load_address_config
    from ovid2.config.connections import load_connection_config
    from ovid2.drivers.registry import DRIVERS, instantiate

    try:
        payload = load_address_config(address_name)
    except (FileNotFoundError, ValueError) as exc:
        logger.debug(
            "collect_for_addresses skipping %r: %s", address_name, exc,
        )
        return ()

    shape = payload.get("_shape")
    if shape == "connection":
        connection_name = str(payload.get("connection") or "")
        if not connection_name:
            return ()
        try:
            conn_raw = load_connection_config(connection_name)
        except (FileNotFoundError, ValueError):
            return ()
        type_name = str(conn_raw.get("type") or "")
        if type_name not in DRIVERS:
            return ()
        args = dict(conn_raw.get("args") or {})
        location = dict(payload.get("location") or {})
        try:
            driver = instantiate(type_name, args=args, secrets={}, location=location)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to instantiate driver %r for address %r; "
                "skipping container-req contribution.",
                type_name, address_name,
            )
            return ()
        try:
            return tuple(driver.container_requirements_for(location))
        except Exception:  # noqa: BLE001
            logger.warning(
                "Driver %r raised while computing container reqs for %r; "
                "skipping.", type_name, address_name,
            )
            return ()

    # Legacy shape.
    src_name = str(payload.get("src_name") or "")
    if src_name not in DRIVERS:
        return ()
    src_args = dict(payload.get("src_args") or {})
    # Split legacy args into the (args, location) shape a driver expects.
    # For the only type that currently contributes mounts (disc), the
    # legacy path lands entirely under ``args`` — the location on the
    # driver stays empty and the mount is derived from the args alone.
    #
    # For non-disc types the driver will return an empty tuple anyway
    # (they don't declare container_requirements_for), so this shape-
    # split is safe as a general default.
    try:
        driver = instantiate(src_name, args=src_args, secrets={}, location={})
    except Exception:  # noqa: BLE001
        return ()
    try:
        return tuple(driver.container_requirements_for({}))
    except Exception:  # noqa: BLE001
        return ()


__all__ = [
    "FORBIDDEN_CONTAINER_PATHS",
    "RESERVED_CONTAINER_PATHS",
    "collect_for_addresses",
    "secret_refs_for_addresses",
]
