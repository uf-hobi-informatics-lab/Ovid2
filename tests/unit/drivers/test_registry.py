"""Unit tests for :mod:`ovid2.drivers.registry`."""

from __future__ import annotations

import pytest

from ovid2.drivers.base import ConnectionFieldSpec, Driver, LocationField, ProbeResult
from ovid2.drivers.registry import (
    DRIVERS,
    instantiate,
    list_types,
    load_class,
    register,
    unregister,
)


class _SampleDriver(Driver):
    type_name = "sample_test"

    @classmethod
    def connection_fields(cls) -> ConnectionFieldSpec:
        return ConnectionFieldSpec()

    @classmethod
    def location_fields(cls) -> tuple[LocationField, ...]:
        return ()

    def probe_auth(self) -> ProbeResult:
        return ProbeResult(ok=True, code="ok", message="ok")


@pytest.fixture(autouse=True)
def _restore_registry() -> None:
    """Snapshot + restore :data:`DRIVERS` around each test."""
    snapshot = dict(DRIVERS)
    yield
    DRIVERS.clear()
    DRIVERS.update(snapshot)


class TestRegister:
    def test_adds_new_type(self) -> None:
        register("sample_test", f"{__name__}._SampleDriver")
        assert "sample_test" in DRIVERS

    def test_idempotent_same_path(self) -> None:
        register("sample_test", f"{__name__}._SampleDriver")
        register("sample_test", f"{__name__}._SampleDriver")
        assert DRIVERS["sample_test"] == f"{__name__}._SampleDriver"

    def test_refuses_conflicting_path(self) -> None:
        register("sample_test", f"{__name__}._SampleDriver")
        with pytest.raises(ValueError, match="already registered"):
            register("sample_test", "other.module.Other")


class TestUnregister:
    def test_removes_existing(self) -> None:
        register("sample_test", f"{__name__}._SampleDriver")
        unregister("sample_test")
        assert "sample_test" not in DRIVERS

    def test_silent_when_absent(self) -> None:
        unregister("never_existed")  # no exception


class TestLoadClass:
    def test_returns_registered_class(self) -> None:
        register("sample_test", f"{__name__}._SampleDriver")
        cls = load_class("sample_test")
        assert cls is _SampleDriver

    def test_raises_for_unknown_type(self) -> None:
        with pytest.raises(KeyError, match="Unknown driver"):
            load_class("nope")


class TestInstantiate:
    def test_produces_driver_instance(self) -> None:
        register("sample_test", f"{__name__}._SampleDriver")
        d = instantiate("sample_test", args={"a": 1}, secrets={"s": 2}, location={"l": 3})
        assert isinstance(d, _SampleDriver)
        assert d.args == {"a": 1}
        assert d.secrets == {"s": 2}
        assert d.location == {"l": 3}


class TestListTypes:
    def test_returns_sorted_keys(self) -> None:
        register("b_test", f"{__name__}._SampleDriver")
        register("a_test", f"{__name__}._SampleDriver")
        types = list_types()
        assert "a_test" in types and "b_test" in types
        assert types == sorted(types)
