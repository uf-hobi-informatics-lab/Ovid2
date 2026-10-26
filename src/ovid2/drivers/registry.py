"""Driver registry — single source of truth for peripheral types.

Post-refactor, every consumer that needs to know "what connectors are
available" or "what class implements the ``s3`` connector" looks here.
The four legacy catalogues
(``connections.registry.CONNECTION_TYPE_MAP``,
``connections.registry._FIELD_SPECS``,
``connections.registry._LOCATION_SPECS``,
``config.addresses.ADDRESS_TYPE_MAP``,
``config.addresses.CONNECTION_TO_SRC_NAME``) become *derived views* on
this registry — see :mod:`ovid2.connections.registry` and
:mod:`ovid2.config.addresses` for the shim wiring.

Registration model
------------------

Drivers register themselves in a single dict, ``DRIVERS``. The value
is a dotted class path (``ovid2.drivers.s3.driver.S3Driver``) rather
than an imported class so that:

- The web tier and the CLI can enumerate the registry at import time
  without loading pyspark / boto / azure / snowflake.
- Concrete driver classes stay free to import their SDKs lazily
  inside methods.

During phases D2–D3 the registry mixes native driver classes with
:class:`~ovid2.drivers.legacy_adapter.LegacyDriverAdapter` factories
wrapping the pre-refactor :class:`BaseConnection` / :class:`BaseAddress`
pairs. Both surfaces implement the :class:`Driver` protocol, so
callers never need to branch.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from ovid2.drivers.base import Driver

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: Type name → dotted path of either a :class:`Driver` subclass **or** a
#: zero-arg factory returning a :class:`Driver` subclass. The factory
#: form is what :func:`ovid2.drivers.legacy_adapter.build_legacy_driver_class`
#: uses to synthesise a driver class from a
#: (:class:`BaseConnection`, :class:`BaseAddress` or ``None``) pair.
DRIVERS: dict[str, str] = {}


def register(type_name: str, dotted_path: str) -> None:
    """Add ``type_name → dotted_path`` to the registry.

    Idempotent: re-registering the same path is a no-op. Registering
    a *different* path under an already-taken name raises
    :class:`ValueError` — a stale registration is far more likely to
    be a bug than an intentional override.
    """
    existing = DRIVERS.get(type_name)
    if existing is None:
        DRIVERS[type_name] = dotted_path
        return
    if existing == dotted_path:
        return
    raise ValueError(
        f"Driver type '{type_name}' is already registered to '{existing}'; "
        f"refusing to overwrite with '{dotted_path}'. If you meant to "
        "replace the entry, delete the old registration first."
    )


def unregister(type_name: str) -> None:
    """Remove ``type_name`` from the registry.

    Intended for tests that need to swap a driver mid-run. Silent
    when the type is not present.
    """
    DRIVERS.pop(type_name, None)


def list_types() -> list[str]:
    """Return the sorted list of registered type names."""
    return sorted(DRIVERS.keys())


def load_class(type_name: str) -> type[Driver]:
    """Import and return the registered class or factory for ``type_name``.

    Returns:
        Either the concrete :class:`Driver` subclass (import + attr
        lookup) or a zero-arg factory. In practice callers just call
        it: ``load_class('s3')(args, secrets, location)`` works for
        both forms because the class *is* callable.
    """
    if type_name not in DRIVERS:
        raise KeyError(f"Unknown driver type '{type_name}'. Registered: {list_types()}.")
    module_path, attr = DRIVERS[type_name].rsplit(".", 1)
    module = importlib.import_module(module_path)
    return cast("type[Driver]", getattr(module, attr))


def instantiate(
    type_name: str,
    args: dict[str, Any] | None = None,
    secrets: dict[str, Any] | None = None,
    location: dict[str, Any] | None = None,
) -> Driver:
    """One-shot: look up the driver class and construct an instance."""
    cls = load_class(type_name)
    return cls(args=args, secrets=secrets, location=location)


def driver_from_address(
    address_name: str,
    *,
    pipeline: str | None = None,
    sf_overrides: dict[str, Any] | None = None,
    cdm_name: str | None = None,
) -> Driver:
    """Resolve a named address JSON to a native :class:`Driver` instance.

    Bridges the legacy on-disk address shapes into the driver
    registry. Handles both shapes:

    - **W7** (``{connection, location}``): loads the referenced
      connection, extracts its ``args`` + ``secrets``, and constructs
      the driver with the address's ``location`` payload.
    - **Legacy** (``{src_name, src_args}``): folds every ``src_args``
      key into the driver's ``args`` slot and leaves ``secrets`` /
      ``location`` empty. Native drivers derive their I/O config
      through :meth:`Driver.as_legacy_src_args`, which merges all
      three slots — so a legacy address whose ``src_args`` already
      carries the merged shape produces the same downstream
      behaviour it did through :func:`~ovid2.config.addresses.load_address`.

    Snowflake's four-tier config resolution (CLI overrides, per-
    address, per-pipeline default, global default) is applied to the
    merged ``src_args`` when ``src_name == 'snowflake'`` — same
    behaviour :func:`~ovid2.config.addresses.load_address` provides
    today, so ``driver_from_address`` is a drop-in replacement for
    callers that want a :class:`Driver` instead of a
    :class:`BaseAddress`.

    Args:
        address_name: Filename stem of a config under ``configs/``.
        pipeline: Optional pipeline name (used for per-pipeline
            Snowflake defaults).
        sf_overrides: Optional CLI-provided Snowflake overrides.
        cdm_name: Optional CDM stamp. Threaded onto the driver's
            ``location`` under the ``cdm`` key so the D6.0 advisory
            type check + Snowflake typed-DDL both see it.

    Returns:
        A native :class:`Driver` instance ready to accept
        :meth:`read` / :meth:`write` calls.

    Raises:
        FileNotFoundError: When ``address_name`` cannot be resolved
            to a JSON config under any search path.
        ValueError: When the config is malformed or references a
            connection type that has no matching driver.
    """
    # Local imports so this module keeps its lean top level and can
    # be imported by ``drivers.legacy_adapter`` at init time without
    # dragging config.addresses into the cycle.
    from ovid2.config import addresses as _addresses
    from ovid2.config import connections as _connections

    config = _addresses.load_address_config(address_name)
    shape = config.get("_shape")

    if shape == "connection":
        connection_name = str(config.get("connection") or "")
        if not connection_name:
            raise ValueError(f"Address '{address_name}' has empty 'connection' field.")
        raw = _connections.load_connection_config(connection_name)
        type_name = str(raw.get("type") or "")
        conn_args = dict(raw.get("args") or {})
        conn_secrets = dict(_connections.resolve_secrets(raw))
        location = dict(config.get("location") or {})
        if cdm_name:
            location["cdm"] = cdm_name
        if type_name == "snowflake":
            # Apply the four-tier resolver against a synthesised
            # legacy-shape merge (args + secrets + location) so the
            # driver's ``as_legacy_src_args`` sees the resolved
            # database / warehouse / role. The resolver writes back
            # onto ``conn_args`` so subsequent driver construction
            # picks it up.
            merged = {**conn_args, **conn_secrets, **location}
            resolved = _addresses._resolve_snowflake_config(
                merged,
                sf_overrides,
                pipeline,
            )
            # Fold resolver outputs back into conn_args (the pipeline
            # side of the driver reads ``args`` for connection-level
            # fields and ``location`` for per-address fields —
            # database / schema move into location, warehouse / role /
            # account into args).
            for key in ("database", "schema"):
                if key in resolved:
                    location[key] = resolved[key]
            for key in ("warehouse", "role", "account", "user", "authenticator"):
                if key in resolved and key not in conn_args:
                    conn_args[key] = resolved[key]
        return instantiate(
            type_name,
            args=conn_args,
            secrets=conn_secrets,
            location=location,
        )

    # Legacy shape.
    src_name = str(config.get("src_name") or "")
    if not src_name:
        raise ValueError(f"Address '{address_name}' missing both 'connection' and 'src_name'.")
    src_args = dict(config.get("src_args") or {})
    if src_name == "snowflake":
        src_args = _addresses._resolve_snowflake_config(
            src_args,
            sf_overrides,
            pipeline,
        )
    if cdm_name:
        src_args["cdm"] = cdm_name
    # A legacy ``src_args`` block is the pre-merge shape (args + secrets +
    # location folded into one dict). Native drivers derive their
    # I/O config from :meth:`as_legacy_src_args`, which re-merges the
    # three slots — so credential keys stuffed into ``args`` alone
    # would be dropped by the merger's secret-only lookup. Split them
    # back out using the driver's own field spec so the driver's
    # ``self.secrets`` sees them and the auth-mode classifier + the
    # Hadoop credentials-provider wiring both work.
    args, secrets, location = _split_legacy_src_args(src_name, src_args)
    return instantiate(src_name, args=args, secrets=secrets, location=location)


def driver_from_connection(
    connection_name: str,
    location: dict[str, Any] | None = None,
) -> Driver:
    """Resolve an inline ``{connection, location}`` to a native :class:`Driver`.

    The run-time-selection counterpart to :func:`driver_from_address`
    (used when a run targets a storage account + location without a saved
    address). The location is the exact location of the data.

    Raises:
        FileNotFoundError: When the connection config does not exist.
        ValueError: When the connection type has no registered driver.
    """
    from ovid2.config import connections as _connections

    raw = _connections.load_connection_config(connection_name)
    type_name = str(raw.get("type") or "")
    return instantiate(
        type_name,
        args=dict(raw.get("args") or {}),
        secrets=dict(_connections.resolve_secrets(raw)),
        location=dict(location or {}),
    )


def _split_legacy_src_args(
    type_name: str,
    src_args: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Split a legacy ``src_args`` blob into ``(args, secrets, location)``.

    Uses the driver's own :meth:`connection_fields` +
    :meth:`location_fields` catalogues to classify every key: secrets
    land in ``secrets``, location keys land in ``location``, and
    everything else stays in ``args``.

    Drivers whose field spec is unreachable at import time fall back
    to putting the whole blob into ``args`` — the pre-refactor
    behaviour — so a mis-authored driver never silently drops data.
    """
    try:
        cls = load_class(type_name)
        conn_spec = cls.connection_fields()
        loc_fields = cls.location_fields()
    except Exception:
        return dict(src_args), {}, {}

    secret_names = {f.name for f in conn_spec.secrets}
    location_names = {f.name for f in loc_fields}

    args: dict[str, Any] = {}
    secrets: dict[str, Any] = {}
    location: dict[str, Any] = {}
    for key, value in src_args.items():
        if key in secret_names:
            secrets[key] = value
        elif key in location_names:
            location[key] = value
        else:
            args[key] = value
    return args, secrets, location


# ---------------------------------------------------------------------------
# Auto-population (deferred to D2)
# ---------------------------------------------------------------------------


def _populate_from_legacy() -> None:
    """Populate :data:`DRIVERS` from the legacy connections catalogue.

    Runs at package import time so any consumer that imports
    :mod:`ovid2.drivers.registry` sees a fully-populated registry. The
    concrete work — synthesising :class:`Driver` subclasses from the
    legacy :class:`BaseConnection` + :class:`BaseAddress` pairs — lives
    in :mod:`ovid2.drivers.legacy_adapter`.

    Tolerant of the circular-import case: when ``legacy_adapter`` is
    the module that imported us and is still executing its own
    top-level code, ``register_all_legacy_drivers`` may not exist
    yet. In that case ``legacy_adapter`` is responsible for
    triggering the population itself once its own module init
    finishes. That's the shape the module uses: it imports us to
    call :func:`register`, then defines its own
    ``register_all_legacy_drivers`` at module bottom, then invokes
    it.
    """
    try:
        from ovid2.drivers import legacy_adapter
    except ImportError:
        return
    # ``getattr`` with default protects against partial-init.
    fn = getattr(legacy_adapter, "register_all_legacy_drivers", None)
    if fn is not None:
        fn()


_populate_from_legacy()


__all__ = [
    "DRIVERS",
    "driver_from_address",
    "instantiate",
    "list_types",
    "load_class",
    "register",
    "unregister",
]
