"""Contract: the engine registry and the web-safe config catalogue agree.

Two modules describe the set of registered engine types:

* :data:`ovid2.core.engines.registry.ENGINE_TYPE_MAP` — pipeline-side,
  imports pyspark / snowpark lazily.
* :data:`ovid2.config.engines._KNOWN_TYPES` and ``_FIELD_SPECS`` —
  web-safe, never imports pyspark / snowpark.

The web tier consults the config module to enumerate types on the
``GET /api/v1/engines/types`` endpoint and validate ``EngineWrite``
payloads. The loader consults the registry module to instantiate the
concrete class. If the two disagree we get orphan types (visible in
the UI but unloadable) or shadow types (loadable but hidden from the
UI). This test refuses either drift.
"""

from __future__ import annotations

import pytest

from ovid2.config import engines as _config
from ovid2.core.engines.registry import ENGINE_TYPE_MAP


@pytest.mark.contract
def test_engine_registry_and_config_agree() -> None:
    """Every engine type known to the class registry appears in the config catalogue."""
    class_side = set(ENGINE_TYPE_MAP)
    config_side = set(_config.list_engine_types())
    only_in_registry = class_side - config_side
    only_in_config = config_side - class_side
    assert not only_in_registry, (
        f"Engine types missing from ovid2.config.engines._FIELD_SPECS: "
        f"{sorted(only_in_registry)}. Add a field spec for each new type."
    )
    assert not only_in_config, (
        f"Engine types missing from ovid2.core.engines.registry.ENGINE_TYPE_MAP: "
        f"{sorted(only_in_config)}. Add the dotted class path for each new type."
    )


@pytest.mark.contract
def test_engine_field_spec_shape_is_stable() -> None:
    """Every engine type's field spec has the ``{args, secrets}`` two-list shape."""
    for type_name in _config.list_engine_types():
        spec = _config.engine_field_spec(type_name)
        assert set(spec.keys()) == {"args", "secrets"}, (
            f"Engine '{type_name}' field spec has unexpected keys: {sorted(spec.keys())}"
        )
        for field in list(spec["args"]) + list(spec["secrets"]):
            assert "name" in field, (
                f"Engine '{type_name}' has a field with no 'name': {field}"
            )
            assert isinstance(field["name"], str), (
                f"Engine '{type_name}' field name must be str: {field}"
            )
