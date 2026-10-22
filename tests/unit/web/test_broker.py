"""Unit tests for the executor-subprocess broker helper."""

from __future__ import annotations

import subprocess

import pytest

from ovid2.web.services import _broker
from ovid2.web.services._broker import BrokerError, run_tool_json


class _Completed:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.mark.unit
def test_parses_last_json_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """The last JSON object on stdout is returned; earlier noise ignored."""
    def _run(argv, **kw):  # noqa: ANN001, ANN003
        return _Completed(
            stdout='log noise\nnot json\n{"ok": true, "code": "ok"}\n'
        )

    monkeypatch.setattr(subprocess, "run", _run)
    result = run_tool_json(["probe-connection", "x"])
    assert result == {"ok": True, "code": "ok"}


@pytest.mark.unit
def test_argv_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """The broker invokes ``python -m ovid2 tools <args>``."""
    captured: dict[str, object] = {}

    def _run(argv, **kw):  # noqa: ANN001, ANN003
        captured["argv"] = argv
        return _Completed(stdout='{"ok": true}')

    monkeypatch.setattr(subprocess, "run", _run)
    run_tool_json(["browse-storage", "acct", "--prefix", "p"])
    argv = captured["argv"]
    assert argv[1:] == ["-m", "ovid2", "tools", "browse-storage", "acct", "--prefix", "p"]


@pytest.mark.unit
def test_timeout_raises_broker_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _run(argv, **kw):  # noqa: ANN001, ANN003
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1.0)

    monkeypatch.setattr(subprocess, "run", _run)
    with pytest.raises(BrokerError, match="timed out"):
        run_tool_json(["probe-connection", "x"], timeout=1.0)


@pytest.mark.unit
def test_launch_failure_raises_broker_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _run(argv, **kw):  # noqa: ANN001, ANN003
        raise OSError("no such executable")

    monkeypatch.setattr(subprocess, "run", _run)
    with pytest.raises(BrokerError, match="Could not launch"):
        run_tool_json(["probe-connection", "x"])


@pytest.mark.unit
def test_no_json_raises_broker_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _run(argv, **kw):  # noqa: ANN001, ANN003
        return _Completed(stdout="only logs, no json", stderr="boom", returncode=1)

    monkeypatch.setattr(subprocess, "run", _run)
    with pytest.raises(BrokerError, match="no JSON result"):
        run_tool_json(["probe-connection", "x"])


@pytest.mark.unit
def test_default_timeout_is_exposed() -> None:
    assert _broker.DEFAULT_TIMEOUT_SECONDS > 0
