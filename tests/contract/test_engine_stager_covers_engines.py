"""Contract: every registered engine yields an :class:`EngineStager`.

Phase A+B of the engine/storage-orthogonality plan moved the
data-plane bridge onto the engine (via :meth:`BaseEngine.get_stager`).
This test locks in the requirement that every entry in
:data:`ovid2.core.engines.registry.ENGINE_TYPE_MAP` — the runtime
registry consulted by :func:`ovid2.core.loaders.load_engine` — resolves
to a concrete engine class whose family has a registered stager.

The check is deliberately class-level (does not instantiate the
engines): a real :class:`~ovid2.core.engines.pyspark.PySparkEngine`
would import pyspark at construction time, and we want the contract
to hold on a host without pyspark installed.

Additionally, this test enforces that the web-safe engine family map
(:data:`ovid2.config.engines._ENGINE_FAMILY`) covers every registered
engine type — the two catalogues must stay in sync.
"""

from __future__ import annotations

from ovid2.core.engines.protocols import EngineFamily
from ovid2.core.engines.registry import ENGINE_TYPE_MAP, load_engine_class
from ovid2.core.engines.stager import (
    EngineStager,
    stager_class_for_engine_family,
    stager_class_for_engine_type,
)


def test_every_registered_engine_class_has_a_family_stager() -> None:
    """Every ``ENGINE_TYPE_MAP`` entry's family must have a stager.

    We import the engine *class* only (no instantiation), read its
    class-level ``family`` attribute, and look up the corresponding
    stager class. A missing entry means Phase A+B forgot to wire the
    family through :func:`stager_class_for_engine_family`.
    """
    for type_name in sorted(ENGINE_TYPE_MAP):
        cls = load_engine_class(type_name)
        family_attr = getattr(cls, "family", None)
        assert family_attr is not None, f"Engine class {cls.__name__} has no 'family' attribute."
        family_value = (
            family_attr.value if isinstance(family_attr, EngineFamily) else str(family_attr)
        )
        stager_cls = stager_class_for_engine_family(family_value)
        assert stager_cls is not None, (
            f"Engine {type_name!r} (family={family_value!r}) has no "
            f"registered stager. Add an entry to "
            f"ovid2.core.engines.stager._FAMILY_TO_STAGER."
        )
        assert issubclass(stager_cls, EngineStager)


def test_every_registered_engine_type_resolves_via_type_name_lookup() -> None:
    """The web-safe type-name variant covers every runtime type.

    :func:`ovid2.core.engines.stager.stager_class_for_engine_type` is
    what the web tier's compat check consults. It goes through
    :func:`ovid2.config.engines.engine_family`, so this test also
    exercises the invariant that ``_ENGINE_FAMILY`` and
    ``ENGINE_TYPE_MAP`` stay aligned.
    """
    for type_name in sorted(ENGINE_TYPE_MAP):
        stager_cls = stager_class_for_engine_type(type_name)
        assert stager_cls is not None, (
            f"stager_class_for_engine_type({type_name!r}) returned None. "
            f"Either ovid2.config.engines._ENGINE_FAMILY is missing this "
            f"key, or its family has no stager registered."
        )
        assert issubclass(stager_cls, EngineStager)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Contract test; run with pytest.")
