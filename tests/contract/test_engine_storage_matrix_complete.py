"""Contract: every engine reaches every storage type (engine universality).

Ovid2's rule (``adr-engine-universality.md``): any registered engine runs
any pipeline on any registered storage type. This test pins the full
**engine × driver** matrix from the live registries, so a new engine or a
new driver is covered automatically — nobody edits a list:

* every engine type in :data:`ovid2.core.engines.registry.ENGINE_TYPE_MAP`
  (and the web-safe mirror in :mod:`ovid2.config.engines`) resolves to a
  stager, and that stager ``supports`` every driver in
  :data:`ovid2.drivers.registry.DRIVERS`;
* the plan-time compat check (:mod:`ovid2.drivers.engine_compat`) accepts
  every engine × driver pairing — including named engine configs, which
  used to skip the check silently;
* a read-only driver (``Driver.read_only``, Access) is refused as an
  *output* by every engine with the same message, and accepted as input.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ovid2.config.engines import _KNOWN_TYPES
from ovid2.core.engines.registry import ENGINE_TYPE_MAP
from ovid2.core.engines.stager import stager_class_for_engine_type
from ovid2.drivers.registry import DRIVERS, load_class

pytestmark = pytest.mark.contract


def test_engine_catalogues_agree() -> None:
    assert set(ENGINE_TYPE_MAP) == set(_KNOWN_TYPES)


@pytest.mark.parametrize("engine_type", sorted(ENGINE_TYPE_MAP))
@pytest.mark.parametrize("driver_type", sorted(DRIVERS))
def test_every_engine_stager_supports_every_driver(engine_type: str, driver_type: str) -> None:
    stager = stager_class_for_engine_type(engine_type)
    assert stager is not None, f"engine {engine_type!r} has no stager"
    assert stager.supports(driver_type), (
        f"engine {engine_type!r} cannot reach storage {driver_type!r}: add a native strategy "
        "or a DuckDB strategy (the Arrow bridge) — every engine must reach every storage type"
    )


def test_read_only_flag_is_declared_and_shared() -> None:
    """``read_only`` is a driver property — identical for every engine by construction."""
    for driver_type in DRIVERS:
        assert isinstance(load_class(driver_type).read_only, bool)
    assert load_class("access").read_only is True


@pytest.fixture()
def addresses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """One saved address per driver type (connection + location)."""
    conns = tmp_path / "connections"
    configs = tmp_path / "configs"
    engines = tmp_path / "engines"
    for d in (conns, configs, engines):
        d.mkdir()
    monkeypatch.setenv("OVID2_CONNECTIONS_DIR", str(conns))
    monkeypatch.setenv("OVID2_CONFIGS_DIR", str(configs))
    monkeypatch.setenv("OVID2_ENGINES_DIR", str(engines))
    names: dict[str, str] = {}
    for driver_type in DRIVERS:
        (conns / f"c_{driver_type}.json").write_text(
            json.dumps({"type": driver_type, "args": {}}), encoding="utf-8"
        )
        (configs / f"a_{driver_type}.json").write_text(
            json.dumps({"connection": f"c_{driver_type}", "location": {}}), encoding="utf-8"
        )
        names[driver_type] = f"a_{driver_type}"
    for engine_type in ENGINE_TYPE_MAP:
        (engines / f"named_{engine_type}.json").write_text(
            json.dumps({"type": engine_type, "args": {}}), encoding="utf-8"
        )
    return names


@pytest.mark.parametrize("engine_type", sorted(ENGINE_TYPE_MAP))
def test_compat_check_accepts_every_pairing(addresses: dict[str, str], engine_type: str) -> None:
    from ovid2.drivers.engine_compat import assert_engine_type_supports_drivers

    for engine_name in (engine_type, f"named_{engine_type}"):
        for src in addresses.values():
            for dst_type, dst in addresses.items():
                if load_class(dst_type).read_only:
                    continue
                assert_engine_type_supports_drivers(engine_name, [src, dst], output_names=[dst])


@pytest.mark.parametrize("engine_type", sorted(ENGINE_TYPE_MAP))
def test_read_only_output_refused_identically(addresses: dict[str, str], engine_type: str) -> None:
    from ovid2.drivers.engine_compat import assert_engine_type_supports_drivers

    read_only = [t for t in DRIVERS if load_class(t).read_only]
    assert read_only
    for driver_type in read_only:
        dst = addresses[driver_type]
        src = addresses["disc"]
        for engine_name in (engine_type, f"named_{engine_type}"):
            with pytest.raises(RuntimeError, match="is read-only"):
                assert_engine_type_supports_drivers(engine_name, [src, dst], output_names=[dst])
            # As an *input* it is fine on every engine.
            assert_engine_type_supports_drivers(engine_name, [dst, src], output_names=[src])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
