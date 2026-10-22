"""Unit tests for :mod:`ovid2.core.engines.registry` and the base classes."""

from __future__ import annotations

import pytest

from ovid2.core.engines import (
    ENGINE_TYPE_MAP,
    BaseEngine,
    EngineFamily,
    EngineOperationUnsupported,
    list_engine_types,
    load_engine_class,
)


@pytest.mark.unit
def test_registry_lists_three_types() -> None:
    """The registry ships three engine types out of the box."""
    types = list_engine_types()
    assert set(types) == {"pyspark", "databricks_spark", "snowpark"}
    # Sorted for stable ordering in the web UI.
    assert types == sorted(types)


@pytest.mark.unit
def test_registry_dotted_paths_are_resolvable() -> None:
    """Every dotted path in ENGINE_TYPE_MAP resolves to a BaseEngine subclass."""
    for type_name in ENGINE_TYPE_MAP:
        cls = load_engine_class(type_name)
        assert issubclass(cls, BaseEngine), (
            f"'{type_name}' resolves to {cls!r} which is not a BaseEngine subclass"
        )
        # type_name attribute must match the registry key.
        assert cls.type_name == type_name


@pytest.mark.unit
def test_registry_rejects_unknown_type() -> None:
    """Loading an unknown type raises KeyError with the known list."""
    with pytest.raises(KeyError, match="Unknown engine type"):
        load_engine_class("does_not_exist")


@pytest.mark.unit
def test_snowpark_declares_reduced_capabilities() -> None:
    """Snowpark advertises its Spark-only holes so callers can gate."""
    cls = load_engine_class("snowpark")
    engine = cls()
    assert engine.family is EngineFamily.SNOWPARK
    assert engine.supports_pandas_udf is False
    assert engine.supports_broadcast_hint is False
    assert engine.supports_input_file_name is False
    assert engine.supports_local_files is False
    assert engine.session_is_spark is False


@pytest.mark.unit
def test_pyspark_and_databricks_share_spark_family() -> None:
    """Both Spark implementations declare family=SPARK and no capability holes."""
    for type_name in ("pyspark", "databricks_spark"):
        engine = load_engine_class(type_name)()
        assert engine.family is EngineFamily.SPARK
        assert engine.supports_pandas_udf is True
        assert engine.supports_broadcast_hint is True
        assert engine.supports_input_file_name is True
        assert engine.session_is_spark is True


@pytest.mark.unit
def test_engine_operation_unsupported_has_useful_message() -> None:
    """The Spark-only-op exception carries the engine name + operation."""
    exc = EngineOperationUnsupported(
        "snowpark", "input_file_name", "no analog in Snowpark"
    )
    assert "snowpark" in str(exc)
    assert "input_file_name" in str(exc)
    assert exc.engine_type == "snowpark"
    assert exc.operation == "input_file_name"


@pytest.mark.unit
def test_base_engine_test_default_returns_not_implemented() -> None:
    """The default test() reports ``not_implemented`` for subclasses that don't override."""

    class _Dummy(BaseEngine):
        type_name = "dummy"

        def get_session(self):  # type: ignore[override]
            return object()

        def stop_session(self) -> None:  # type: ignore[override]
            return None

    result = _Dummy().test()
    assert result.ok is False
    assert result.code == "not_implemented"
