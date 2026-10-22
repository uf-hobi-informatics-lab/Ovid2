"""Unit tests for :mod:`ovid2.drivers.probe`."""

from __future__ import annotations

from typing import Any

from ovid2.drivers.base import (
    ConnectionFieldSpec,
    Driver,
    LocationField,
    ProbeResult,
)
from ovid2.drivers.probe import run_all_probes, run_test


class _BaseStub(Driver):
    type_name = "stub"

    @classmethod
    def connection_fields(cls) -> ConnectionFieldSpec:
        return ConnectionFieldSpec()

    @classmethod
    def location_fields(cls) -> tuple[LocationField, ...]:
        return ()

    def probe_auth(self) -> ProbeResult:
        return ProbeResult(ok=True, code="ok", message="auth-ok")


class _RaisingDriver(_BaseStub):
    def probe_auth(self) -> ProbeResult:
        raise RuntimeError("boom")


class _WrongShapeDriver(_BaseStub):
    def probe_auth(self) -> Any:
        return "not-a-probe-result"


class _WrongCapabilityDriver(_BaseStub):
    """Driver that returns ``capability='write'`` from ``probe_auth``."""

    def probe_auth(self) -> ProbeResult:
        return ProbeResult(
            ok=True,
            code="ok",
            message="ok",
            capability="write",
        )


class _FullDriver(_BaseStub):
    def probe_read(self) -> ProbeResult:
        return ProbeResult(ok=True, code="ok", message="read-ok", capability="read")

    def probe_write(self) -> ProbeResult:
        return ProbeResult(ok=False, code="auth", message="write-denied", capability="write")


class TestRunAllProbes:
    def test_stub_missing_optionals(self) -> None:
        results = run_all_probes(_BaseStub())
        assert [r.capability for r in results] == ["auth", "read", "write"]
        assert results[0].ok is True and results[0].code == "ok"
        assert results[1].ok is False and results[1].code == "not_implemented"
        assert results[2].ok is False and results[2].code == "not_implemented"

    def test_full_driver_reports_each_capability(self) -> None:
        results = run_all_probes(_FullDriver())
        assert results[0].ok is True and results[0].code == "ok"
        assert results[1].ok is True and results[1].code == "ok"
        assert results[2].ok is False and results[2].code == "auth"

    def test_raising_driver_becomes_error_result(self) -> None:
        results = run_all_probes(_RaisingDriver())
        auth = results[0]
        assert auth.ok is False
        assert auth.code == "error"
        assert "RuntimeError" in auth.message
        assert "boom" in auth.message
        assert auth.capability == "auth"

    def test_wrong_shape_becomes_error_result(self) -> None:
        results = run_all_probes(_WrongShapeDriver())
        auth = results[0]
        assert auth.code == "error"
        assert "ProbeResult" in auth.message

    def test_wrong_capability_is_corrected(self) -> None:
        results = run_all_probes(_WrongCapabilityDriver())
        assert results[0].capability == "auth"


class TestRunTest:
    def test_returns_legacy_test_result_shape(self) -> None:
        result = run_test(_BaseStub())
        # ``ConnectionTestResult`` has ok / code / message / detail — but
        # no ``capability``.
        assert result.ok is True
        assert result.code == "ok"
        assert result.message == "auth-ok"
        assert result.detail == {}
        assert not hasattr(result, "capability")

    def test_raising_driver_downgrades_gracefully(self) -> None:
        result = run_test(_RaisingDriver())
        assert result.ok is False
        assert result.code == "error"
