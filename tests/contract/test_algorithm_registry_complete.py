"""Contract: ``AlgorithmRegistry`` is the one entry point for algorithms.

1. **Complete and exact.** Every entry in each native registry is reachable
   through the facade, and the facade exposes nothing the native registries
   do not hold:

   * formatter ↔ ``formatters.adapters.ADAPTERS`` (aliases included);
   * deduplicator ↔ the one composite-key ``Deduplicator``;
   * mapper ↔ ``mappers.registry.MAP_OPS``;
   * fixer ↔ ``transformations.fixers.common.registry.COMMON_FIXES`` (incl.
     domain-pack fixers merged by ``_load_pack_fixers``);
   * transform ↔ ``transforms.registry.TRANSFORMS``;
   * health_check ↔ ``transformations.quality.registry.load_all_specs()``.

2. **No side doors.** Outside ``src/ovid2/algorithms/`` and the native
   registries' own packages, no module imports a per-kind registry (or the
   native lookups ``get_operator`` / ``select_specs`` / ``Deduplicator``).
   Pipeline code, stage transformers, CLI and tools resolve through the
   facade.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.contract._ast_utils import dynamic_imports, iter_imports

#: Modules / names that *are* a per-kind registry or native lookup.
NATIVE_REGISTRY_IMPORTS: frozenset[str] = frozenset(
    {
        "ovid2.formatters.adapters.ADAPTERS",
        "ovid2.formatters.adapters.ADAPTER_ALIASES",
        "ovid2.mappers.registry",
        "ovid2.mappers.MAP_OPS",
        "ovid2.transforms.registry",
        "ovid2.transforms.TRANSFORMS",
        "ovid2.transformations.fixers.common.registry",
        "ovid2.transformations.quality.registry.get_spec",
        "ovid2.transformations.quality.registry.select_specs",
        "ovid2.transformations.quality.registry.load_all_specs",
        "ovid2.transformations.quality.ops.get_operator",
        "ovid2.transformations.quality.ops.operator_names",
        "ovid2.transformations.deduplicators.base",
        "ovid2.transformations.deduplicators.base.Deduplicator",
    }
)

#: Where those imports are legitimate: the facade and each registry's own
#: package (which defines / re-exports it).
_OWNERS: tuple[str, ...] = (
    "algorithms/",
    "formatters/adapters/__init__.py",
    "mappers/__init__.py",
    "mappers/registry.py",
    "transforms/__init__.py",
    "transforms/registry.py",
    "transformations/fixers/common/",
    "transformations/quality/registry.py",
    "transformations/quality/ops/",
    "transformations/deduplicators/",
)


def test_facade_is_complete_and_exact() -> None:
    """Facade names == native registry names, kind by kind."""
    from ovid2.algorithms.protocol import Algorithm, AlgorithmKind
    from ovid2.algorithms.registry import DEDUPLICATOR_NAME, registry
    from ovid2.formatters.adapters import ADAPTERS
    from ovid2.mappers.registry import MAP_OPS
    from ovid2.transformations.deduplicators.base import Deduplicator
    from ovid2.transformations.fixers.common.registry import COMMON_FIXES
    from ovid2.transformations.quality.registry import load_all_specs
    from ovid2.transforms.registry import TRANSFORMS

    native = {
        AlgorithmKind.FORMATTER: dict(ADAPTERS),
        AlgorithmKind.DEDUPLICATOR: {DEDUPLICATOR_NAME: Deduplicator},
        AlgorithmKind.MAPPER: dict(MAP_OPS),
        AlgorithmKind.FIXER: dict(COMMON_FIXES),
        AlgorithmKind.TRANSFORM: dict(TRANSFORMS),
        AlgorithmKind.HEALTH_CHECK: {s.id: s for s in load_all_specs()},
    }
    assert set(native) == set(AlgorithmKind), "a kind has no native registry mapping"
    for kind, entries in native.items():
        assert set(registry.names(kind)) == set(entries), kind
        for name, obj in entries.items():
            algorithm = registry.get(kind, name)
            assert isinstance(algorithm, Algorithm), (kind, name)
            assert algorithm.kind is kind
            assert algorithm.native is obj, (kind, name)
            assert isinstance(algorithm.requires, frozenset)
    total = sum(len(e) for e in native.values())
    assert len(registry.list()) == total


def test_facade_preserves_native_resolution_rules() -> None:
    """Aliases, fixer scopes and check names resolve as they do natively."""
    from ovid2.algorithms.registry import UnknownAlgorithm, registry
    from ovid2.transformations.fixers.common.registry import FIXER_ALIASES, scope_of
    from ovid2.transformations.quality.registry import load_all_specs

    assert registry.describe("formatter", "pcornet_passthrough")["alias_of"] == (
        "source_passthrough"
    )
    for name in registry.names("fixer"):
        info = registry.describe("fixer", name)
        assert info["scope"] == scope_of(name)
        assert info["aliases"] == FIXER_ALIASES.get(name, {})
    spec = load_all_specs()[0]
    assert registry.get("health_check", spec.name).name == spec.id
    with pytest.raises(UnknownAlgorithm):
        registry.get("mapper", "no_such_op")
    with pytest.raises(ValueError):
        registry.register("health_check", "x", object())


def test_register_delegates_to_native_registry() -> None:
    """``register`` writes into the native dict (and refuses collisions)."""
    from ovid2.algorithms.registry import registry
    from ovid2.mappers.registry import MAP_OPS

    existing = registry.native("mapper", "literal")
    with pytest.raises(ValueError):
        registry.register("mapper", "literal", existing)
    registry.register("mapper", "_contract_probe", existing)
    try:
        assert MAP_OPS["_contract_probe"] is existing
        assert registry.has("mapper", "_contract_probe")
    finally:
        MAP_OPS.pop("_contract_probe", None)


def _offenders(src_root: Path) -> list[str]:
    out: list[str] = []
    for py in sorted(src_root.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        rel = py.relative_to(src_root).as_posix()
        if rel.startswith(_OWNERS):
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for ref in iter_imports(tree):
            if not ref.type_checking and ref.module in NATIVE_REGISTRY_IMPORTS:
                out.append(f"src/ovid2/{rel}:{ref.lineno} {ref.module}")
        for dyn in dynamic_imports(tree):
            if dyn.module in NATIVE_REGISTRY_IMPORTS:
                out.append(f"src/ovid2/{rel}:{dyn.lineno} importlib {dyn.module}")
    return out


def test_no_direct_per_kind_registry_imports(src_root: Path) -> None:
    """Everything outside the facade resolves algorithms through it."""
    offenders = _offenders(src_root)
    assert not offenders, (
        "Resolve algorithms through ovid2.algorithms.registry.AlgorithmRegistry, "
        "not a per-kind registry:\n" + "\n".join(offenders)
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("pytest contract module; not executable.")
