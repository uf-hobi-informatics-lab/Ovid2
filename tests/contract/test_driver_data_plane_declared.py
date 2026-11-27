"""Contract: every driver honestly declares its data plane.

Before this test, ``SparkEngineStager.supports()`` returned ``True`` for
every registered driver, but the four hybrid drivers (``postgres`` /
``mssql`` / ``gcs`` / ``databricks``) had no legacy ``BaseAddress``
bridge, so a Spark ``read`` / ``write`` raised a misleading
``ValueError`` deep inside a running job.

Invariants:

1. Every driver declares ``spark_capable`` (its *native* Spark data
   plane) as a ``bool``, and it agrees with
   :meth:`SparkEngineStager.native_supports`. Drivers without a native
   Spark plane are still reachable from Spark through the Arrow bridge
   (engine universality — see ``test_engine_storage_matrix_complete.py``).
2. Every driver is reachable by every engine stager.
3. A non-Spark-capable driver's *direct* Spark ``read`` / ``write``
   (bypassing the stager) fails with an actionable
   ``NotImplementedError`` — never the registry-internal ``ValueError``.
4. Every native driver module imports cleanly (no silent fallback to the
   legacy adapter — see ``legacy_adapter.register_all_legacy_drivers``).
"""

from __future__ import annotations

import importlib

import pytest

from ovid2.core.engines.duckdb_stager import DuckDBEngineStager
from ovid2.core.engines.stager import SnowparkEngineStager, SparkEngineStager
from ovid2.drivers.registry import DRIVERS, load_class

_NATIVE_MODULES = (
    "ovid2.drivers.disc",
    "ovid2.drivers.s3",
    "ovid2.drivers.azure_blob",
    "ovid2.drivers.snowflake",
    "ovid2.drivers.access",
    "ovid2.drivers.postgres",
    "ovid2.drivers.mssql",
    "ovid2.drivers.databricks",
    "ovid2.drivers.gcs",
)


@pytest.mark.contract
@pytest.mark.parametrize("type_name", sorted(DRIVERS))
def test_driver_declares_spark_capability(type_name: str) -> None:
    """``spark_capable`` is a bool and agrees with the Spark stager's native plane."""
    cls = load_class(type_name)
    assert isinstance(cls.spark_capable, bool)
    assert SparkEngineStager.native_supports(type_name) is cls.spark_capable


@pytest.mark.contract
@pytest.mark.parametrize("type_name", sorted(DRIVERS))
def test_every_driver_has_some_data_plane(type_name: str) -> None:
    """Every engine stager can materialise / persist this driver."""
    reachable = {
        "spark": SparkEngineStager.supports(type_name),
        "duckdb": DuckDBEngineStager.supports(type_name),
        "snowpark": SnowparkEngineStager.supports(type_name),
    }
    assert all(reachable.values()), f"driver {type_name!r} unreachable: {reachable}"


@pytest.mark.contract
@pytest.mark.parametrize("type_name", sorted(t for t in DRIVERS if not load_class(t).spark_capable))
def test_non_spark_driver_fails_actionably(type_name: str) -> None:
    """Direct Spark ``read`` / ``write`` on a non-native driver point at the stager."""
    driver = load_class(type_name)(args={}, secrets={}, location={})
    with pytest.raises(NotImplementedError, match="stager"):
        driver.read(object(), "p", "b", "t")  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError, match="stager"):
        driver.write(object(), object(), "p", "b", "t")  # type: ignore[arg-type]


@pytest.mark.contract
@pytest.mark.parametrize("module_path", _NATIVE_MODULES)
def test_native_driver_module_imports(module_path: str) -> None:
    """A broken native driver must fail here, not fall back silently."""
    importlib.import_module(module_path)
    type_name = module_path.rsplit(".", 1)[1]
    registered = DRIVERS[type_name]
    assert not registered.startswith("ovid2.drivers.legacy_adapter."), (
        f"{type_name} is registered as the bare legacy adapter ({registered}); "
        "its native module failed to register."
    )
    assert registered.startswith(module_path + "."), registered
