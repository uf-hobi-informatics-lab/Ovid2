"""Contract: capability declarations are complete, exact and cannot go stale.

1. **Closed set ↔ surface.** ``Capability`` has exactly one ``fn:<name>``
   member per function in ``surface.FUNCTIONS`` (+ the Ovid2 extensions)
   plus the declared feature members — nothing ad hoc.
2. **Engines declare.** Every registered engine's ``capabilities()`` is a
   subset of ``Capability``, includes every function capability (engine
   universality) and equals the web-safe mirror
   ``ovid2.config.capabilities.ENGINE_CAPABILITIES``.
3. **Algorithms declare what they use.** For every algorithm in the
   ``AlgorithmRegistry`` the capabilities AST-collected from its source
   (:func:`ovid2.algorithms.requirements.used_capabilities`) are a subset of
   its ``requires``.
4. **Web-safe mirror is exact.** ``ovid2.config.algorithm_requirements``
   lists exactly each algorithm's declared *feature* capabilities.
"""

from __future__ import annotations

import pytest

from ovid2.config.capabilities import (
    ENGINE_CAPABILITIES,
    FEATURE_CAPABILITIES,
    FUNCTION_CAPABILITIES,
    FUNCTION_EXTENSIONS,
    FUNCTION_PREFIX,
    Capability,
)


def test_capabilities_mirror_the_portable_surface() -> None:
    from ovid2.core.engines import surface

    expected = {f"{FUNCTION_PREFIX}{n}" for n in (*surface.FUNCTIONS, *FUNCTION_EXTENSIONS)}
    assert {c.value for c in FUNCTION_CAPABILITIES} == expected
    assert frozenset(Capability) == FEATURE_CAPABILITIES | FUNCTION_CAPABILITIES
    assert {c.value for c in FEATURE_CAPABILITIES} == {
        "window",
        "regex",
        "checkpoint",
        "broadcast_join",
        "rand_seeded",
        "sample_with_replacement",
        "sha2_all_bits",
        "map_from_columns",
        "input_file_name",
    }


@pytest.mark.parametrize("engine_type", sorted(ENGINE_CAPABILITIES))
def test_engine_declares_capabilities(engine_type: str) -> None:
    from ovid2.core.engines.registry import ENGINE_TYPE_MAP, load_engine_class

    assert set(ENGINE_CAPABILITIES) == set(ENGINE_TYPE_MAP)
    engine = load_engine_class(engine_type)()
    declared = engine.capabilities()
    assert declared <= frozenset(Capability)
    assert declared >= FUNCTION_CAPABILITIES, "every engine implements the whole surface"
    assert declared == ENGINE_CAPABILITIES[engine_type]


def _algorithms() -> list[tuple[str, str]]:
    from ovid2.algorithms.registry import registry

    return [(a.kind.value, a.name) for a in registry.list()]


@pytest.mark.parametrize(("kind", "name"), _algorithms())
def test_algorithm_requires_covers_what_it_calls(kind: str, name: str) -> None:
    from ovid2.algorithms.registry import registry
    from ovid2.algorithms.requirements import used_capabilities

    algorithm = registry.get(kind, name)
    native = getattr(algorithm, "operator", None) if kind == "health_check" else algorithm.native
    if native is None:
        pytest.skip(f"{kind}:{name} has no implementation to scan")
    used = {c.value for c in used_capabilities(native)}
    missing = used - set(algorithm.requires)
    assert not missing, (
        f"{kind}:{name} uses {sorted(missing)} but does not declare them in "
        "REQUIRES / @operator(requires=…)"
    )


def test_web_safe_feature_mirror_is_exact() -> None:
    from ovid2.algorithms.registry import registry
    from ovid2.config.algorithm_requirements import ALGORITHM_FEATURES

    live: dict[str, dict[str, frozenset[str]]] = {}
    for algorithm in registry.list():
        feats = frozenset(r for r in algorithm.requires if not r.startswith(FUNCTION_PREFIX))
        if feats:
            live.setdefault(algorithm.kind.value, {})[algorithm.name] = feats
    mirror = {k: v for k, v in ALGORITHM_FEATURES.items() if v}
    assert mirror == live, (
        "ovid2.config.algorithm_requirements.ALGORITHM_FEATURES is out of sync with the "
        "algorithms' REQUIRES declarations"
    )


def test_param_features_match_requires_for() -> None:
    from ovid2.algorithms.registry import registry
    from ovid2.config.algorithm_requirements import PARAM_FEATURES

    for (kind, name), pairs in PARAM_FEATURES.items():
        algorithm = registry.get(kind, name)
        for param, feature in pairs:
            value = 1 if param == "seed" else True
            assert feature in algorithm.requires_for({param: value}), (kind, name, param)  # type: ignore[attr-defined]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("pytest contract module; not executable.")
