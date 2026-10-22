"""Legacy → Driver adapter.

Wraps the pre-Driver-refactor :class:`ovid2.connections.base.BaseConnection`
and :class:`ovid2.core.addresses.base.BaseAddress` pair as a single
:class:`~ovid2.drivers.base.Driver` so callers can consume the new
registry uniformly during the D2–D5 migration window.

The adapter is deliberately conservative:

- **Metadata classmethods** read from the existing
  ``ovid2.connections.registry`` catalogues (``_FIELD_SPECS``,
  ``_LOCATION_SPECS``, ``CONNECTION_TYPE_MAP``). No heavy imports.
- **Probe** delegates to :meth:`BaseConnection.test` and adapts the
  legacy :class:`ConnectionTestResult` to a :class:`ProbeResult`.
- **Data plane** (:meth:`read` / :meth:`write` /
  :meth:`create_complementary_tables`) lazily imports the
  :class:`BaseAddress` subclass and delegates to it.
- **Legacy bridge** (:meth:`as_legacy_src_args`) returns the exact
  ``(src_name, src_args)`` shape :func:`ovid2.config.addresses.load_address`
  already expects — the old machinery keeps working unchanged.

A native :class:`Driver` port (D3.<type>) replaces the adapter entry
for that type in :data:`ovid2.drivers.registry.DRIVERS` verbatim; no
downstream consumer needs to know.

Container requirements
----------------------

The adapter routes ``container_requirements_for`` through a per-type
hook table (:data:`_CONTAINER_HOOKS`) so the disc auto-mount logic —
which today lives in :mod:`ovid2.connections.disc_mounts` — surfaces
here without pulling that module into every driver. Types with no
hook return an empty tuple.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from ovid2.drivers.base import (
    ConnectionFieldSpec,
    Driver,
    FieldSpec,
    LocationField,
    ProbeResult,
)
from ovid2.drivers.container import ContainerRequirement
from ovid2.drivers.registry import register

if TYPE_CHECKING:
    from ovid2.connections.base import BaseConnection
    from ovid2.core.addresses.base import BaseAddress

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Metadata bridge
# ---------------------------------------------------------------------------

def _connection_field_spec_from_legacy(type_name: str) -> ConnectionFieldSpec:
    """Read ``_FIELD_SPECS[type_name]`` and rebuild it as a typed spec."""
    from ovid2.connections.registry import connection_field_spec

    raw = connection_field_spec(type_name)
    return ConnectionFieldSpec(
        args=tuple(_field_from_dict(f) for f in raw.get("args", [])),
        secrets=tuple(_field_from_dict(f) for f in raw.get("secrets", [])),
        note=raw.get("note"),
    )


def _location_fields_from_legacy(type_name: str) -> tuple[LocationField, ...]:
    """Read ``_LOCATION_SPECS[type_name]`` and rebuild it as a tuple."""
    from ovid2.connections.registry import location_field_spec

    raw = location_field_spec(type_name)
    return tuple(_field_from_dict(f, cls=LocationField) for f in raw)


def _field_from_dict(f: dict[str, Any], *, cls: type = FieldSpec) -> Any:
    """Reshape a legacy ``{name, required, default, help, widget}`` dict."""
    # Location specs carry a ``type`` field for the HTML form renderer;
    # store it in ``widget`` so :class:`FieldSpec` can hold both.
    widget = f.get("widget") or f.get("type") or "str"
    if f.get("multiline"):
        widget = "multiline"
    return cls(
        name=str(f.get("name", "")),
        required=bool(f.get("required", False)),
        default=f.get("default"),
        help=str(f.get("help", "")),
        widget=str(widget),
    )


# ---------------------------------------------------------------------------
# Container requirements bridge
# ---------------------------------------------------------------------------

_ContainerHook = Callable[[dict[str, Any], dict[str, Any]], tuple[ContainerRequirement, ...]]


#: Per-type container-requirement hooks used by the legacy adapter.
#:
#: Empty as of D3.disc — the disc driver's native
#: :meth:`DiscDriver.container_requirements_for` owns the mount-derivation
#: logic now, and the D5 collector's legacy path calls
#: :func:`ovid2.drivers.disc.metadata.container_requirements_from` when a
#: pre-W7 ``{src_name: disc, src_args: {path: /x}}`` address is
#: encountered. Kept as an (empty) dict so future non-native types can
#: still register a hook here during their D3 grace window if they need
#: one.
_CONTAINER_HOOKS: dict[str, _ContainerHook] = {}


# ---------------------------------------------------------------------------
# Adapter class factory
# ---------------------------------------------------------------------------

def build_legacy_driver_class(type_name: str) -> type[Driver]:
    """Return a :class:`Driver` subclass that adapts the given legacy type.

    The returned class is memoized in-module so
    ``build_legacy_driver_class('s3')`` is idempotent across calls.
    """
    if type_name in _CLASS_CACHE:
        return _CLASS_CACHE[type_name]

    class _LegacyDriverAdapter(Driver):
        """Wraps a legacy connection+address pair as a Driver."""

        # Set at class-creation time (below).
        type_name: str = ""

        # ---------- Metadata ----------

        @classmethod
        def connection_fields(cls) -> ConnectionFieldSpec:
            return _connection_field_spec_from_legacy(cls.type_name)

        @classmethod
        def location_fields(cls) -> tuple[LocationField, ...]:
            return _location_fields_from_legacy(cls.type_name)

        @classmethod
        def declared_cdm(cls, location: dict[str, Any] | None) -> str | None:
            """Return the CDM stamped on an address's location, if any.

            D6.0 advisory type stamping: the pipeline reads ``cdm`` off
            an address's ``location`` (or, for legacy shape, from
            ``src_args``) and uses it to select the typed DDL. Route
            validation logs a warning when source and target addresses
            disagree — strict enforcement is opt-in (D6.1) via
            ``strict_types: true`` on the route.

            Only Snowflake declares a CDM today (via its
            ``_cdm_name`` attribute). Other types return ``None``. The
            classmethod signature accepts ``location`` so per-instance
            state doesn't leak out.
            """
            if not location:
                return None
            value = location.get("cdm")
            if value is None:
                return None
            return str(value)

        def container_requirements_for(
            self, location: dict[str, Any] | None = None,
        ) -> tuple[ContainerRequirement, ...]:
            hook = _CONTAINER_HOOKS.get(self.type_name)
            if hook is None:
                return ()
            loc = dict(location or self.location or {})
            return hook(dict(self.args), loc)

        # ---------- Probe ----------

        def _build_legacy_connection(self) -> BaseConnection:
            """Import and construct the pre-refactor connection class."""
            from ovid2.connections.registry import load_connection_class

            connection_cls = load_connection_class(self.type_name)
            return connection_cls(args=dict(self.args), secrets=dict(self.secrets))

        def probe_auth(self) -> ProbeResult:
            try:
                legacy = self._build_legacy_connection()
            except Exception as exc:  # noqa: BLE001
                return ProbeResult(
                    ok=False, code="error",
                    message=f"Could not build legacy connection: {exc}",
                    capability="auth",
                )
            try:
                result = legacy.test()
            except Exception as exc:  # noqa: BLE001
                return ProbeResult(
                    ok=False, code="error",
                    message=f"probe raised: {exc.__class__.__name__}: {exc}",
                    capability="auth",
                )
            return ProbeResult(
                ok=result.ok,
                code=result.code,
                message=result.message,
                capability="auth",
                detail=dict(result.detail),
            )

        # ---------- Legacy bridge ----------

        def _resolved_src_args(self) -> dict[str, Any]:
            """Merge args+secrets+location the way the legacy loader would.

            Delegates to :meth:`BaseConnection.as_address_config` so
            per-type quirks (Azure secret ordering, disc container_path
            resolution) are respected verbatim.
            """
            legacy = self._build_legacy_connection()
            return dict(legacy.as_address_config(dict(self.location)))

        def as_legacy_src_args(self) -> tuple[str, dict[str, Any]]:
            from ovid2.config.addresses import CONNECTION_TO_SRC_NAME

            src_name = CONNECTION_TO_SRC_NAME.get(self.type_name, self.type_name)
            return src_name, self._resolved_src_args()

        # ---------- Data plane ----------

        def _build_legacy_address(self) -> BaseAddress:
            """Lazily import + construct the matching legacy address class."""
            from ovid2.config.addresses import ADDRESS_TYPE_MAP
            import importlib

            src_name, src_args = self.as_legacy_src_args()
            if src_name not in ADDRESS_TYPE_MAP:
                raise ValueError(
                    f"Legacy adapter for '{self.type_name}' resolves to "
                    f"address type '{src_name}' which is not registered "
                    "in ADDRESS_TYPE_MAP."
                )
            module_path, class_name = ADDRESS_TYPE_MAP[src_name].rsplit(".", 1)
            module = importlib.import_module(module_path)
            address_cls = getattr(module, class_name)
            return address_cls(src_args)  # type: ignore[no-any-return]

        def read(self, session, pipeline, batch_id, file_name, delimiter=None, *,
                 table_name=None):
            address = self._build_legacy_address()
            return address.read(
                session, pipeline, batch_id, file_name, delimiter,
                table_name=table_name,
            )

        def write(self, df, session, pipeline, batch_id, table_name,
                  delimiter=None):
            address = self._build_legacy_address()
            address.write(df, session, pipeline, batch_id, table_name, delimiter)

        def create_complementary_tables(self, session, pipeline, batch_id):
            address = self._build_legacy_address()
            return address.create_complementary_tables(session, pipeline, batch_id)

    _LegacyDriverAdapter.type_name = type_name
    _LegacyDriverAdapter.__name__ = f"LegacyDriverAdapter_{type_name}"
    _LegacyDriverAdapter.__qualname__ = _LegacyDriverAdapter.__name__
    _CLASS_CACHE[type_name] = _LegacyDriverAdapter
    return _LegacyDriverAdapter


#: Adapter class memoization. Keeps ``load_class(type)`` idempotent.
_CLASS_CACHE: dict[str, type[Driver]] = {}


# ---------------------------------------------------------------------------
# Auto-population
# ---------------------------------------------------------------------------

def register_all_legacy_drivers() -> None:
    """Register a :class:`LegacyDriverAdapter` for every legacy type.

    Called at :mod:`ovid2.drivers.registry` import time. Idempotent:
    :func:`ovid2.drivers.registry.register` no-ops when the dotted
    path is unchanged.

    Types that have a native :class:`Driver` in ``ovid2.drivers.<type>``
    (D3 migrations) are **skipped** — the native module self-registers
    at its own import time and the legacy adapter must never overwrite
    the native entry with the pre-D3 shim.

    Each remaining type name maps to a dedicated adapter class installed
    as a top-level attribute on this module (``LegacyDriverAdapter_<type>``).
    The class itself is cheap to build — no SDK imports fire until an
    instance actually calls :meth:`probe_auth` or :meth:`read` — so
    pre-building all N adapters at registry-init time is fine.
    """
    from ovid2.connections.registry import CONNECTION_TYPE_MAP
    from ovid2.drivers.registry import DRIVERS

    # Native driver modules to import first so they self-register in
    # ``DRIVERS`` before the loop below runs. Each D3.<type> PR appends
    # its own module here. Skipped silently if the module is missing
    # (e.g. mid-refactor / partial checkout).
    _NATIVE_DRIVER_MODULES = (  # noqa: N806 — module-local constant
        "ovid2.drivers.disc",
        "ovid2.drivers.s3",
        "ovid2.drivers.azure_blob",
        "ovid2.drivers.snowflake",
        "ovid2.drivers.access",
    )
    for module_path in _NATIVE_DRIVER_MODULES:
        try:
            __import__(module_path)
        except Exception:  # noqa: BLE001 — init must never propagate
            # A broken native driver falls back to the legacy adapter
            # for that type on the next loop iteration.
            pass

    for type_name in CONNECTION_TYPE_MAP:
        # Skip types already registered by a native driver module (D3+).
        # ``register()`` would raise ``ValueError`` otherwise because
        # the native dotted path differs from the adapter's.
        if type_name in DRIVERS:
            continue
        cls = build_legacy_driver_class(type_name)
        # Install a stable top-level attribute the registry can resolve
        # via ``importlib.import_module`` + ``getattr``.
        setattr(_module_self(), cls.__name__, cls)
        register(
            type_name,
            f"ovid2.drivers.legacy_adapter.{cls.__name__}",
        )


def _module_self() -> Any:
    """Return this module object (for :func:`setattr`)."""
    import sys

    return sys.modules[__name__]


__all__ = [
    "build_legacy_driver_class",
    "register_all_legacy_drivers",
]


# Self-populate at module init so ``from ovid2.drivers import registry``
# lands on a fully-loaded ``DRIVERS`` regardless of import order.
# ``registry._populate_from_legacy`` runs first (triggered by
# ``from .registry import register`` above) but at that point
# ``register_all_legacy_drivers`` isn't defined yet, so it silently
# no-ops. Once this module finishes defining it, we do the actual
# registration here.
try:
    register_all_legacy_drivers()
except Exception:  # noqa: BLE001 — module init must never propagate
    # Registry stays empty; callers will get a KeyError with a
    # useful message from load_class(). This branch is only hit if
    # the connections registry itself failed to import, which is a
    # separate bug worth surfacing on the next probe attempt.
    pass
