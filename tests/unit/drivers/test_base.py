"""Unit tests for :mod:`ovid2.drivers.base`."""

from __future__ import annotations

import pytest

from ovid2.drivers.base import (
    ConnectionFieldSpec,
    Driver,
    FieldSpec,
    LocationField,
    ProbeResult,
)


class _StubDriver(Driver):
    """Minimum viable driver for unit tests."""

    type_name = "stub"

    @classmethod
    def connection_fields(cls) -> ConnectionFieldSpec:
        return ConnectionFieldSpec(
            args=(FieldSpec(name="host", required=True, help="Host."),),
            secrets=(FieldSpec(name="token", required=False, help="Token."),),
            note="stub note",
        )

    @classmethod
    def location_fields(cls) -> tuple[LocationField, ...]:
        return (
            LocationField(name="path", required=False, help="Sub-path."),
        )

    def probe_auth(self) -> ProbeResult:
        return ProbeResult(ok=True, code="ok", message="stub-auth")


class TestFieldSpec:
    def test_to_dict_minimum(self) -> None:
        assert FieldSpec(name="x", required=True).to_dict() == {
            "name": "x", "required": True,
        }

    def test_to_dict_full(self) -> None:
        got = FieldSpec(
            name="x", required=False, default="a", help="help",
            widget="path_picker",
        ).to_dict()
        assert got == {
            "name": "x", "required": False, "default": "a",
            "help": "help", "widget": "path_picker",
        }


class TestConnectionFieldSpec:
    def test_to_dict_round_trips_args_and_secrets(self) -> None:
        spec = ConnectionFieldSpec(
            args=(FieldSpec(name="a", required=True),),
            secrets=(FieldSpec(name="s", required=True),),
            note="hi",
        )
        got = spec.to_dict()
        assert got["args"] == [{"name": "a", "required": True}]
        assert got["secrets"] == [{"name": "s", "required": True}]
        assert got["note"] == "hi"

    def test_to_dict_omits_note_when_absent(self) -> None:
        spec = ConnectionFieldSpec()
        assert "note" not in spec.to_dict()


class TestProbeResult:
    def test_default_capability_is_auth(self) -> None:
        r = ProbeResult(ok=True, code="ok", message="ok")
        assert r.capability == "auth"
        assert r.detail == {}

    def test_to_legacy_test_result_drops_capability(self) -> None:
        r = ProbeResult(
            ok=False, code="auth", message="denied", capability="read",
            detail={"account": "x"},
        )
        legacy = r.to_legacy_test_result()
        assert legacy.ok is False
        assert legacy.code == "auth"
        assert legacy.message == "denied"
        assert legacy.detail == {"account": "x"}
        # No ``capability`` on the legacy shape.
        assert not hasattr(legacy, "capability")


class TestDriverConstruction:
    def test_defaults_to_empty_dicts(self) -> None:
        d = _StubDriver()
        assert d.args == {}
        assert d.secrets == {}
        assert d.location == {}

    def test_stores_copies_not_references(self) -> None:
        args = {"host": "example"}
        d = _StubDriver(args=args)
        args["host"] = "mutated"
        assert d.args["host"] == "example"

    def test_none_arguments_become_empty_dicts(self) -> None:
        d = _StubDriver(args=None, secrets=None, location=None)
        assert d.args == {} and d.secrets == {} and d.location == {}


class TestDriverDefaults:
    def test_container_requirements_default_is_empty(self) -> None:
        assert _StubDriver.container_requirements() == ()

    def test_container_requirements_for_defaults_to_static(self) -> None:
        d = _StubDriver()
        assert d.container_requirements_for({"path": "x"}) == ()

    def test_cdm_dimension_name_defaults_to_type_name(self) -> None:
        assert _StubDriver.cdm_dimension_name() == "stub"

    def test_declared_cdm_defaults_to_none(self) -> None:
        assert _StubDriver.declared_cdm(None) is None
        assert _StubDriver.declared_cdm({"cdm": "pcornet_7_0"}) is None


class TestDriverOptionalCapabilities:
    def test_probe_read_raises_not_implemented_by_default(self) -> None:
        with pytest.raises(NotImplementedError):
            _StubDriver().probe_read()

    def test_probe_write_raises_not_implemented_by_default(self) -> None:
        with pytest.raises(NotImplementedError):
            _StubDriver().probe_write()

    def test_read_raises_not_implemented_by_default(self) -> None:
        with pytest.raises(NotImplementedError):
            _StubDriver().read(session=None, pipeline="p", batch_id="b", file_name="f")

    def test_write_raises_not_implemented_by_default(self) -> None:
        with pytest.raises(NotImplementedError):
            _StubDriver().write(
                df=None, session=None, pipeline="p", batch_id="b", table_name="t",
            )

    def test_create_complementary_tables_is_a_noop(self) -> None:
        assert _StubDriver().create_complementary_tables(None, "p", "b") is None


class TestDriverLegacyBridge:
    def test_as_legacy_src_args_merges_all_three_dicts(self) -> None:
        d = _StubDriver(
            args={"host": "h"},
            secrets={"token": "t"},
            location={"path": "p"},
        )
        src, merged = d.as_legacy_src_args()
        assert src == "stub"
        assert merged == {"host": "h", "token": "t", "path": "p"}

    def test_as_legacy_src_args_location_overrides_args(self) -> None:
        """Location wins over args when keys collide."""
        d = _StubDriver(args={"path": "a"}, location={"path": "b"})
        _, merged = d.as_legacy_src_args()
        assert merged["path"] == "b"
