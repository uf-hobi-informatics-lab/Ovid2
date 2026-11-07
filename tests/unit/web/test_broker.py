"""Unit tests for the executor-subprocess broker helper."""

from __future__ import annotations

import subprocess

import pytest

from ovid2.web.services import _broker
from ovid2.web.services._broker import BrokerError, SecretScope, run_tool_json

_S = SecretScope(connections=("x",))


class _Completed:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.mark.unit
def test_parses_last_json_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """The last JSON object on stdout is returned; earlier noise ignored."""

    def _run(argv, **kw):
        return _Completed(stdout='log noise\nnot json\n{"ok": true, "code": "ok"}\n')

    monkeypatch.setattr(subprocess, "run", _run)
    result = run_tool_json(["probe-connection", "x"], scope=_S)
    assert result == {"ok": True, "code": "ok"}


@pytest.mark.unit
def test_argv_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """The broker invokes ``python -m ovid2 tools <args>``."""
    captured: dict[str, object] = {}

    def _run(argv, **kw):
        captured["argv"] = argv
        return _Completed(stdout='{"ok": true}')

    monkeypatch.setattr(subprocess, "run", _run)
    run_tool_json(["browse-storage", "acct", "--prefix", "p"], scope=_S)
    argv = captured["argv"]
    assert argv[1:] == ["-m", "ovid2", "tools", "browse-storage", "acct", "--prefix", "p"]


@pytest.mark.unit
def test_timeout_raises_broker_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _run(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1.0)

    monkeypatch.setattr(subprocess, "run", _run)
    with pytest.raises(BrokerError, match="timed out"):
        run_tool_json(["probe-connection", "x"], scope=_S, timeout=1.0)


@pytest.mark.unit
def test_launch_failure_raises_broker_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _run(argv, **kw):
        raise OSError("no such executable")

    monkeypatch.setattr(subprocess, "run", _run)
    with pytest.raises(BrokerError, match="Could not launch"):
        run_tool_json(["probe-connection", "x"], scope=_S)


@pytest.mark.unit
def test_no_json_raises_broker_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _run(argv, **kw):
        return _Completed(stdout="only logs, no json", stderr="boom", returncode=1)

    monkeypatch.setattr(subprocess, "run", _run)
    with pytest.raises(BrokerError, match="no JSON result"):
        run_tool_json(["probe-connection", "x"], scope=_S)


@pytest.mark.unit
def test_default_timeout_is_exposed() -> None:
    assert _broker.DEFAULT_TIMEOUT_SECONDS > 0


@pytest.mark.unit
def test_child_env_is_scoped_and_token_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """The child gets only its scope's secrets and no inherited tokens."""
    import os
    from pathlib import Path

    xdg = Path(str(tmp_path)) / "xdg"
    sd = xdg / "ovid2" / "secrets.d"
    sd.mkdir(parents=True)
    for name in ("conn_a", "conn_b"):
        (sd / f"{name}.json").write_text('{"k": "v"}', encoding="utf-8")
        (sd / f"{name}.json").chmod(0o600)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(Path(str(tmp_path)) / "run"))
    monkeypatch.setenv("OVID2_MCP_API_TOKEN", "leak")
    monkeypatch.setenv("OVID2_SECRETS_FILE", "/etc/passwd")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient")
    monkeypatch.setattr("ovid2.config.executor_env.resolve_scope", lambda scope: {"conn_a.json"})
    seen: dict[str, object] = {}

    def _run(argv, **kw):
        env = kw["env"]
        seen["env"] = dict(env)
        scoped = Path(env["OVID2_SECRETS_DIR"])
        seen["files"] = sorted(p.name for p in scoped.iterdir())
        seen["mode"] = oct(scoped.stat().st_mode & 0o777)
        seen["dir"] = scoped
        return _Completed(stdout='{"ok": true}')

    monkeypatch.setattr(subprocess, "run", _run)
    run_tool_json(["probe-connection", "a"], scope=_S)
    env = seen["env"]
    assert isinstance(env, dict)
    assert seen["files"] == ["conn_a.json"]
    assert seen["mode"] == "0o700"
    for leaked in ("OVID2_MCP_API_TOKEN", "OVID2_SECRETS_FILE", "AWS_SECRET_ACCESS_KEY"):
        assert leaked not in env
    assert not Path(str(seen["dir"])).exists(), "scoped dir must be removed after the call"
    assert os.environ.get("OVID2_SECRETS_DIR") is None
