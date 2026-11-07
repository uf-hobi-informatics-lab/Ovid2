"""Unit tests for scoped executor secrets.

Covers :mod:`ovid2.config.secrets` (``OVID2_SECRETS_DIR`` +
warn / enforce), :mod:`ovid2.config.secret_refs` (name-only resolution),
and :mod:`ovid2.config.executor_env` (env filtering + scoped dir).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ovid2.config import executor_env, secret_refs, secrets
from ovid2.config.secret_refs import SecretScope


def _write(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated XDG tree with two connections + one engine + one address."""
    root = tmp_path / "xdg"
    cfg = root / "ovid2"
    _write(cfg / "secrets.d" / "conn_a.json", {"password": "A"})
    _write(cfg / "secrets.d" / "conn_b.json", {"password": "B"})
    _write(cfg / "secrets.d" / "engine_sp.json", {"password": "E"})
    _write(cfg / "secrets.d" / "demo.json", {"id_salt": "S"})
    _write(
        cfg / "connections" / "a.json",
        {"type": "postgres", "args": {"host": "h"}, "secret_ref": "conn_a"},
    )
    _write(
        cfg / "connections" / "b.json",
        {"type": "postgres", "args": {"host": "h"}, "secret_ref": "conn_b"},
    )
    _write(cfg / "configs" / "addr_a.json", {"connection": "a", "location": {}})
    _write(
        cfg / "engines" / "sp.json",
        {"type": "snowpark", "args": {"connection": "b"}, "secret_ref": "engine_sp"},
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    for var in (
        "OVID2_SECRETS_DIR",
        "OVID2_SECRETS_FILE",
        "OVID2_SECRETS_SCOPE_MODE",
        "OVID2_CONNECTIONS_DIR",
        "OVID2_CONFIGS_DIR",
        "OVID2_ENGINES_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    return root


# ---------------------------------------------------------------- secret_refs


@pytest.mark.unit
def test_refs_follow_address_connection_and_engine(xdg: Path) -> None:
    assert secret_refs.refs_for_addresses(["addr_a"]) == {"conn_a"}
    assert secret_refs.refs_for_connections(["b"]) == {"conn_b"}
    # Engine: its own ref + the connection its args name.
    assert secret_refs.refs_for_engines(["sp"]) == {"engine_sp", "conn_b"}
    # Bare engine types have no secret; unknown names contribute nothing.
    assert secret_refs.refs_for_engines(["pyspark", "nope"]) == set()
    assert secret_refs.refs_for_addresses(["missing"]) == set()


@pytest.mark.unit
def test_resolve_scope_returns_file_names(xdg: Path) -> None:
    scope = SecretScope(addresses=("addr_a",), pipelines=("demo",), extra_refs=("xor_seed.txt",))
    assert secret_refs.resolve_scope(scope) == {"conn_a.json", "demo.json", "xor_seed.txt"}


# --------------------------------------------------------------- executor_env


@pytest.mark.unit
def test_build_minimal_env_drops_tokens_and_ambient_creds() -> None:
    source = {
        "PATH": "/bin",
        "OVID2_DATA_DIR": "/d",
        "OVID2_MCP_API_TOKEN": "t",
        "OVID2_LLM_API_TOKEN": "t",
        "OVID2_SECRETS_FILE": "/x",
        "OVID2_SECRETS_DIR": "/parent",
        "AWS_SECRET_ACCESS_KEY": "k",
    }
    env = executor_env.build_minimal_env(source, ("PATH", "OVID2_*"))
    assert env == {"PATH": "/bin", "OVID2_DATA_DIR": "/d"}


@pytest.mark.unit
def test_scoped_env_grants_only_the_scope(xdg: Path) -> None:
    with executor_env.scoped_executor_env(
        SecretScope(addresses=("addr_a",)), allowlist=("XDG_*",)
    ) as env:
        scoped = Path(env["OVID2_SECRETS_DIR"])
        assert sorted(p.name for p in scoped.iterdir()) == ["conn_a.json"]
        assert scoped.stat().st_mode & 0o777 == 0o700
        assert (scoped / "conn_a.json").stat().st_mode & 0o777 == 0o600
    assert not scoped.exists()


@pytest.mark.unit
def test_scoped_dir_removed_on_exception(xdg: Path) -> None:
    holder: list[Path] = []
    with (
        pytest.raises(RuntimeError),
        executor_env.scoped_executor_env(SecretScope(connections=("a",)), allowlist=()) as env,
    ):
        holder.append(Path(env["OVID2_SECRETS_DIR"]))
        raise RuntimeError("boom")
    assert not holder[0].exists()


@pytest.mark.unit
def test_sweep_removes_stale_scopes(xdg: Path) -> None:
    scoped = executor_env.prepare_scoped_secrets(SecretScope(), label="stale")
    old = 1_000_000_000
    os.utime(scoped.path, (old, old))
    assert executor_env.sweep_stale_scopes(max_age_seconds=60) >= 1
    assert not scoped.path.exists()


# ------------------------------------------------------------ load_secret


@pytest.mark.unit
def test_load_secret_prefers_scope(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scoped = executor_env.prepare_scoped_secrets(SecretScope(connections=("a",)))
    try:
        monkeypatch.setenv("OVID2_SECRETS_DIR", str(scoped.path))
        assert secrets.load_secret("conn_a") == {"password": "A"}
    finally:
        scoped.cleanup()


@pytest.mark.unit
def test_warn_mode_falls_back_with_warning(
    xdg: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    scoped = executor_env.prepare_scoped_secrets(SecretScope(connections=("a",)))
    try:
        monkeypatch.setenv("OVID2_SECRETS_DIR", str(scoped.path))
        with caplog.at_level("WARNING", logger="ovid2.config.secrets"):
            assert secrets.load_secret("conn_b") == {"password": "B"}
        assert "outside this executor's declared scope" in caplog.text
    finally:
        scoped.cleanup()


@pytest.mark.unit
def test_enforce_mode_refuses_out_of_scope(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scoped = executor_env.prepare_scoped_secrets(SecretScope(connections=("a",)))
    try:
        monkeypatch.setenv("OVID2_SECRETS_DIR", str(scoped.path))
        monkeypatch.setenv("OVID2_SECRETS_SCOPE_MODE", "enforce")
        # Even OVID2_SECRETS_FILE (answers every name) is ignored under enforce.
        monkeypatch.setenv("OVID2_SECRETS_FILE", str(xdg / "ovid2" / "secrets.d" / "conn_b.json"))
        assert secrets.load_secret("conn_a") == {"password": "A"}
        with pytest.raises(secrets.SecretNotFoundError, match="not granted"):
            secrets.load_secret("conn_b")
    finally:
        scoped.cleanup()


@pytest.mark.unit
def test_secret_name_traversal_rejected(xdg: Path) -> None:
    with pytest.raises(ValueError):
        secrets.load_secret("../conn_a")


@pytest.mark.unit
def test_pipeline_salt_honours_scope(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ovid2.mappers.engine import load_secrets

    scoped = executor_env.prepare_scoped_secrets(SecretScope(connections=("a",)))
    try:
        monkeypatch.setenv("OVID2_SECRETS_DIR", str(scoped.path))
        monkeypatch.setenv("OVID2_SECRETS_SCOPE_MODE", "enforce")
        assert load_secrets("demo") == {}  # not granted -> absent, no fallback
        monkeypatch.setenv("OVID2_SECRETS_SCOPE_MODE", "warn")
        assert load_secrets("demo") == {"id_salt": "S"}  # warn -> fallback
    finally:
        scoped.cleanup()
