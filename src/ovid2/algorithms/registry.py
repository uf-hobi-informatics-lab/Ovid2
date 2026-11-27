"""``AlgorithmRegistry`` — the single entry point for every algorithm.

A **facade**, not a new registry: each kind keeps its own native registry
(dicts, aliases, fixer scopes, pack loading, YAML check loading) and the
facade *delegates* to it. That keeps every existing call path and contract
test working, while pipeline code resolves algorithms one way, mirroring
the storage (:mod:`ovid2.drivers.registry`) and compute
(:mod:`ovid2.core.engines.registry`) handlers.

=================  ==========================================================
Kind               Delegates to
=================  ==========================================================
``formatter``      ``ovid2.formatters.adapters.ADAPTERS`` (+ ``ADAPTER_ALIASES``)
``deduplicator``   ``ovid2.transformations.deduplicators`` (one: ``composite_key``)
``mapper``         ``ovid2.mappers.registry.MAP_OPS``
``fixer``          ``ovid2.transformations.fixers.common.registry.COMMON_FIXES``
                   (+ ``FIXER_ALIASES`` / ``FIXER_SCOPES`` / pack fixers)
``transform``      ``ovid2.transforms.registry.TRANSFORMS``
``health_check``   ``ovid2.transformations.quality.registry`` (YAML ``CheckSpec``)
                   + ``ovid2.transformations.quality.ops`` (operators)
=================  ==========================================================

Every native registry entry is reachable through :meth:`AlgorithmRegistry.get`
and the facade exposes nothing extra
(``tests/contract/test_algorithm_registry_complete.py``). Outside this package
and the native registries' own modules, nothing imports a per-kind registry
(same contract).

Executor tier — the web tier reads the web-safe catalogues
(``ovid2.config.*_catalogue``), which a sync contract pins to this facade.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from ovid2.algorithms.adapters import (
    DeduplicatorAlgorithm,
    FixerAlgorithm,
    FormatterAlgorithm,
    HealthCheckAlgorithm,
    MapperAlgorithm,
    TransformAlgorithm,
    summary_of,
)
from ovid2.algorithms.protocol import Algorithm, AlgorithmKind

#: ``AlgorithmRegistry.list`` shadows the builtin inside the class body.
_List = list

__all__ = ["DEDUPLICATOR_NAME", "AlgorithmRegistry", "UnknownAlgorithm", "registry"]

#: The deduplicator's registered name (there is one implementation).
DEDUPLICATOR_NAME: str = "composite_key"


class UnknownAlgorithm(LookupError):
    """Raised by :meth:`AlgorithmRegistry.get` for an unregistered name."""

    def __init__(self, kind: AlgorithmKind, name: str, known: list[str]) -> None:
        super().__init__(
            f"Unknown {kind.value} '{name}'. Registered: {', '.join(sorted(known)) or '(none)'}."
        )
        self.kind = kind
        self.name = name


# --------------------------------------------------------------------------- #
# Per-kind delegates (lazy imports: importing the facade loads nothing heavy) #
# --------------------------------------------------------------------------- #


def _adapters() -> tuple[dict[str, Any], dict[str, str]]:
    module = importlib.import_module("ovid2.formatters.adapters")
    return module.ADAPTERS, module.ADAPTER_ALIASES


def _map_ops() -> dict[str, Any]:
    return importlib.import_module("ovid2.mappers.registry").MAP_OPS  # type: ignore[no-any-return]


def _fixer_registry() -> Any:
    return importlib.import_module("ovid2.transformations.fixers.common.registry")


def _transforms() -> dict[str, Any]:
    return importlib.import_module("ovid2.transforms.registry").TRANSFORMS  # type: ignore[no-any-return]


def _deduplicators() -> dict[str, Any]:
    module = importlib.import_module("ovid2.transformations.deduplicators.base")
    return {DEDUPLICATOR_NAME: module.Deduplicator}


def _checks() -> tuple[Any, Any]:
    return (
        importlib.import_module("ovid2.transformations.quality.registry"),
        importlib.import_module("ovid2.transformations.quality.ops"),
    )


class AlgorithmRegistry:
    """Facade over the six native algorithm registries."""

    # ------------------------------------------------------------------ #
    # Native views                                                        #
    # ------------------------------------------------------------------ #

    def native_entries(self, kind: AlgorithmKind | str) -> dict[str, Any]:
        """Return ``{name: native object}`` for ``kind`` (aliases included).

        The live native dict is never returned; mutate through
        :meth:`register`.
        """
        kind = AlgorithmKind(kind)
        if kind is AlgorithmKind.FORMATTER:
            return dict(_adapters()[0])
        if kind is AlgorithmKind.DEDUPLICATOR:
            return _deduplicators()
        if kind is AlgorithmKind.MAPPER:
            return dict(_map_ops())
        if kind is AlgorithmKind.FIXER:
            return dict(_fixer_registry().COMMON_FIXES)
        if kind is AlgorithmKind.TRANSFORM:
            return dict(_transforms())
        registry_mod, _ops = _checks()
        return {spec.id: spec for spec in registry_mod.load_all_specs()}

    def names(self, kind: AlgorithmKind | str) -> list[str]:
        """Return the sorted registered names of ``kind``."""
        return sorted(self.native_entries(kind))

    # ------------------------------------------------------------------ #
    # Lookup                                                              #
    # ------------------------------------------------------------------ #

    def get(self, kind: AlgorithmKind | str, name: str) -> Algorithm:
        """Return the algorithm ``name`` of ``kind``.

        Formatter aliases (``pcornet_passthrough``) and health-check names
        (``name`` as well as ``id``) resolve as they do natively.

        Raises:
            UnknownAlgorithm: When ``name`` is not registered.
        """
        kind = AlgorithmKind(kind)
        if kind is AlgorithmKind.HEALTH_CHECK:
            registry_mod, ops = _checks()
            spec = registry_mod.get_spec(name)
            if spec is None:
                raise UnknownAlgorithm(kind, name, self.names(kind))
            return HealthCheckAlgorithm(spec.id, spec, ops.get_operator(spec.operator))
        entries = self.native_entries(kind)
        native = entries.get(name)
        if native is None:
            raise UnknownAlgorithm(kind, name, list(entries))
        wrapper: Callable[..., Algorithm] = _WRAPPERS[kind]
        return wrapper(name, native)

    def has(self, kind: AlgorithmKind | str, name: str) -> bool:
        """Return whether ``name`` is registered under ``kind``."""
        try:
            self.get(kind, name)
        except UnknownAlgorithm:
            return False
        return True

    def native(self, kind: AlgorithmKind | str, name: str) -> Any:
        """Return the native class / spec behind ``(kind, name)``.

        For call sites that still drive the native API (e.g. instantiate a
        fixer and read its attributes).
        """
        algorithm = self.get(kind, name)
        return algorithm.native

    def list(self, kind: AlgorithmKind | str | None = None) -> list[Algorithm]:
        """Return every algorithm (of ``kind`` when given), sorted by kind then name."""
        kinds = [AlgorithmKind(kind)] if kind is not None else list(AlgorithmKind)
        out: list[Algorithm] = []
        for k in kinds:
            out.extend(self.get(k, name) for name in self.names(k))
        return out

    def describe(self, kind: AlgorithmKind | str, name: str) -> dict[str, Any]:
        """Return a JSON-ready description of ``(kind, name)``.

        Keys: ``kind``, ``name``, ``summary``, ``requires`` (sorted list)
        plus kind-specific metadata (fixer ``scope`` / ``aliases``, formatter
        ``alias_of``, transform ``category`` / ``params_schema``, health-check
        ``operator`` / ``part``).
        """
        kind = AlgorithmKind(kind)
        algorithm = self.get(kind, name)
        native = algorithm.native
        info: dict[str, Any] = {
            "kind": kind.value,
            "name": algorithm.name,
            "requires": sorted(algorithm.requires),
        }
        if kind is AlgorithmKind.HEALTH_CHECK:
            info.update(summary=native.name, operator=native.operator, part=native.part)
            return info
        info["summary"] = summary_of(native)
        if kind is AlgorithmKind.FORMATTER:
            info["alias_of"] = _adapters()[1].get(name)
        elif kind is AlgorithmKind.FIXER:
            fixers = _fixer_registry()
            info["scope"] = fixers.scope_of(name)
            info["aliases"] = dict(fixers.FIXER_ALIASES.get(name, {}))
        elif kind is AlgorithmKind.TRANSFORM:
            instance = native()
            info["category"] = getattr(instance, "category", "")
            info["params_schema"] = instance.params_schema
        return info

    # ------------------------------------------------------------------ #
    # Registration (delegated)                                            #
    # ------------------------------------------------------------------ #

    def register(
        self, kind: AlgorithmKind | str, name: str, obj: Any, *, overwrite: bool = False
    ) -> None:
        """Register ``obj`` as ``name`` in the native registry of ``kind``.

        Mapper ops and transforms go through their native ``register``;
        formatter adapters and fixers are added to their native dicts with the
        same collision rule. Health checks are YAML (``resources/health/checks``
        or a domain pack) and the deduplicator is fixed, so both refuse.

        Raises:
            ValueError: On a name collision without ``overwrite`` or a
                non-registrable kind.
        """
        kind = AlgorithmKind(kind)
        if kind is AlgorithmKind.MAPPER:
            importlib.import_module("ovid2.mappers.registry").register(
                name, obj, overwrite=overwrite
            )
            return
        if kind is AlgorithmKind.TRANSFORM:
            importlib.import_module("ovid2.transforms.registry").register(
                name, obj, overwrite=overwrite
            )
            return
        if kind is AlgorithmKind.FORMATTER:
            target = _adapters()[0]
        elif kind is AlgorithmKind.FIXER:
            target = _fixer_registry().COMMON_FIXES
        else:
            raise ValueError(
                f"{kind.value} algorithms are not registered at runtime "
                "(health checks are YAML specs; the deduplicator is fixed)."
            )
        if name in target and not overwrite:
            raise ValueError(f"{kind.value} '{name}' is already registered.")
        target[name] = obj

    # ------------------------------------------------------------------ #
    # Kind-specific helpers the stage transformers need                   #
    # ------------------------------------------------------------------ #

    def normalise_fix_config(self, fix_config: dict[str, object]) -> dict[str, object]:
        """Rewrite a fixer config's named aliases into ``src1`` / ``src2``."""
        result: dict[str, object] = _fixer_registry().normalise_fix_config(fix_config)
        return result

    def fixer_scopes(self) -> dict[str, str]:
        """Return ``{fixer: scope}`` in the native declaration order."""
        return dict(_fixer_registry().FIXER_SCOPES)

    def select_health_checks(
        self,
        *,
        parts: _List[str] | None,
        checks: _List[str] | None,
        packages: _List[str] | None = None,
    ) -> _List[Any]:
        """Return the selected :class:`CheckSpec` objects (native selector)."""
        registry_mod, _ops = _checks()
        selected: _List[Any] = registry_mod.select_specs(
            parts=parts, checks=checks, packages=packages
        )
        return selected

    def health_operator(self, name: str) -> Callable[..., Any] | None:
        """Return the health-check operator callable ``name`` (or ``None``)."""
        _registry_mod, ops = _checks()
        operator: Callable[..., Any] | None = ops.get_operator(name)
        return operator

    def health_operator_names(self) -> frozenset[str]:
        """Return every registered health-check operator name."""
        _registry_mod, ops = _checks()
        names: frozenset[str] = ops.operator_names()
        return names


_WRAPPERS: dict[AlgorithmKind, Callable[..., Algorithm]] = {
    AlgorithmKind.FORMATTER: FormatterAlgorithm,
    AlgorithmKind.DEDUPLICATOR: DeduplicatorAlgorithm,
    AlgorithmKind.MAPPER: MapperAlgorithm,
    AlgorithmKind.FIXER: FixerAlgorithm,
    AlgorithmKind.TRANSFORM: TransformAlgorithm,
}

#: Process-wide facade instance.
registry: AlgorithmRegistry = AlgorithmRegistry()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.algorithms.registry is a library module.")
