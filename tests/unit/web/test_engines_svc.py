"""Unit tests for :mod:`ovid2.web.services.engines_svc`."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ovid2.web.models import EngineWrite


@pytest.mark.unit
def test_list_engine_types_returns_three(isolated_xdg: Path) -> None:
    from ovid2.web.services import engines_svc

    specs = engines_svc.list_engine_types()
    names = [s.type for s in specs]
    assert set(names) == {"pyspark", "databricks_spark", "snowpark"}


@pytest.mark.unit
def test_pyspark_field_spec_has_no_secrets(isolated_xdg: Path) -> None:
    from ovid2.web.services import engines_svc

    specs = engines_svc.list_engine_types()
    pyspark = next(s for s in specs if s.type == "pyspark")
    assert pyspark.secrets == []


@pytest.mark.unit
def test_write_and_read_pyspark_engine(isolated_xdg: Path) -> None:
    from ovid2.web.services import engines_svc

    payload = EngineWrite(
        type="pyspark",
        args={"spark.executor.memory": "48g", "app_name": "T"},
        secrets={},
    )
    result = engines_svc.write_engine("prod_pyspark", payload, actor="test")
    assert result.action == "created"

    detail = engines_svc.get_engine("prod_pyspark")
    assert detail is not None
    assert detail.type == "pyspark"
    assert detail.args["spark.executor.memory"] == "48g"
    assert detail.secrets == {}
    assert detail.secret_ref is None


@pytest.mark.unit
def test_secret_file_is_mode_0o600(isolated_xdg: Path) -> None:
    from ovid2.web.services import engines_svc

    payload = EngineWrite(
        type="databricks_spark",
        args={"workspace_url": "https://a.cloud.databricks.com", "cluster_id": "c-1"},
        secrets={"token": "dapiSECRETVAL"},
    )
    engines_svc.write_engine("db_prod", payload, actor="test")
    detail = engines_svc.get_engine("db_prod")
    assert detail is not None
    assert detail.secret_ref == "engine_db_prod"
    # The token round-trips as the masked sentinel.
    assert detail.secrets == {"token": "***"}
    # And the on-disk secret file must be 0o600.
    secrets_dir = Path(os.environ["XDG_CONFIG_HOME"]) / "ovid2" / "secrets.d"
    secret_path = secrets_dir / "engine_db_prod.json"
    assert secret_path.is_file()
    mode = oct(secret_path.stat().st_mode & 0o777)
    assert mode == "0o600", f"Secret file mode is {mode}, expected 0o600"


@pytest.mark.unit
def test_sentinel_preserves_existing_secret(isolated_xdg: Path) -> None:
    from ovid2.web.services import engines_svc

    payload = EngineWrite(
        type="databricks_spark",
        args={"workspace_url": "https://a.databricks.com"},
        secrets={"token": "original"},
    )
    engines_svc.write_engine("db_test", payload)

    # Update using the sentinel — original value should survive.
    updated = EngineWrite(
        type="databricks_spark",
        args={"workspace_url": "https://a.databricks.com", "cluster_id": "c-999"},
        secrets={"token": "***"},
    )
    engines_svc.write_engine("db_test", updated)

    # Read the raw secret file directly (the API never surfaces it).
    secrets_dir = Path(os.environ["XDG_CONFIG_HOME"]) / "ovid2" / "secrets.d"
    raw = json.loads((secrets_dir / "engine_db_test.json").read_text())
    assert raw["token"] == "original"


@pytest.mark.unit
def test_write_rejects_inline_secret(isolated_xdg: Path) -> None:
    from ovid2.web.services import engines_svc

    payload = EngineWrite(
        type="databricks_spark",
        args={"workspace_url": "https://a.databricks.com", "token": "SECRET"},
        secrets={},
    )
    with pytest.raises(engines_svc.EngineError, match="are secrets"):
        engines_svc.write_engine("db_leaky", payload)


@pytest.mark.unit
def test_write_rejects_unknown_arg(isolated_xdg: Path) -> None:
    from ovid2.web.services import engines_svc

    payload = EngineWrite(
        type="pyspark",
        args={"bogus_key": "x"},
        secrets={},
    )
    with pytest.raises(engines_svc.EngineError, match="Unknown args"):
        engines_svc.write_engine("bogus", payload)


@pytest.mark.unit
def test_write_rejects_missing_required(isolated_xdg: Path) -> None:
    from ovid2.web.services import engines_svc

    payload = EngineWrite(
        type="databricks_spark",
        args={},  # workspace_url is required
        secrets={"token": "x"},
    )
    with pytest.raises(engines_svc.EngineError, match="workspace_url"):
        engines_svc.write_engine("db_incomplete", payload)


@pytest.mark.unit
def test_create_only_conflict_raises(isolated_xdg: Path) -> None:
    from ovid2.web.services import engines_svc

    payload = EngineWrite(type="pyspark", args={}, secrets={})
    engines_svc.write_engine("dup", payload, create_only=True)
    with pytest.raises(engines_svc.EngineError, match="already exists"):
        engines_svc.write_engine("dup", payload, create_only=True)


@pytest.mark.unit
def test_delete_removes_config(isolated_xdg: Path) -> None:
    from ovid2.web.services import engines_svc

    engines_svc.write_engine("to_delete", EngineWrite(type="pyspark", args={}, secrets={}))
    result = engines_svc.delete_engine("to_delete")
    assert result.action == "deleted"
    assert engines_svc.get_engine("to_delete") is None


@pytest.mark.unit
def test_delete_refuses_when_referenced_by_settings(isolated_xdg: Path) -> None:
    from ovid2.web.services import engines_svc, settings_svc

    engines_svc.write_engine(
        "referenced", EngineWrite(type="pyspark", args={}, secrets={})
    )
    settings = settings_svc.get_settings()
    settings.defaults.engine = "referenced"
    settings_svc.save_settings(settings)

    with pytest.raises(engines_svc.EngineError, match="settings.defaults.engine"):
        engines_svc.delete_engine("referenced")


@pytest.mark.unit
def test_delete_missing_raises(isolated_xdg: Path) -> None:
    from ovid2.web.services import engines_svc

    with pytest.raises(engines_svc.EngineError, match="not found"):
        engines_svc.delete_engine("does_not_exist")


@pytest.mark.unit
def test_invalid_name_rejected(isolated_xdg: Path) -> None:
    from ovid2.web.services import engines_svc

    with pytest.raises(engines_svc.EngineError, match="Invalid engine name"):
        engines_svc.write_engine(
            "bad/name",
            EngineWrite(type="pyspark", args={}, secrets={}),
        )


@pytest.mark.unit
def test_reserved_name_rejected(isolated_xdg: Path) -> None:
    from ovid2.web.services import engines_svc

    with pytest.raises(engines_svc.EngineError, match="reserved"):
        engines_svc.write_engine(
            "foo_sample",
            EngineWrite(type="pyspark", args={}, secrets={}),
        )


@pytest.mark.unit
def test_audit_row_written(isolated_xdg: Path) -> None:
    """Every write leaves a row in web_audit."""
    from ovid2.web import db as web_db
    from ovid2.web.services import engines_svc

    engines_svc.write_engine(
        "audited", EngineWrite(type="pyspark", args={}, secrets={}), actor="alice"
    )
    with web_db.connect() as conn:
        rows = list(conn.execute("SELECT actor, action, target FROM web_audit"))
    assert any(
        r["action"] == "engine.created" and r["target"] == "audited" and r["actor"] == "alice"
        for r in rows
    )


# ---------------------------------------------------------------------------
# Test probe dispatch (E1 post-fix): pyspark → container, others → host.
# ---------------------------------------------------------------------------


def _fake_subprocess_run(script):
    """Return a stand-in for :func:`subprocess.run`.

    ``script`` is a callable ``(argv, **kwargs) -> subprocess.CompletedProcess``
    that the test uses to record the argv and pick a canned reply.
    """
    import subprocess as _subprocess

    def _run(argv, **kwargs):
        stdout, stderr, returncode = script(argv, **kwargs)
        return _subprocess.CompletedProcess(
            args=argv,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    return _run


@pytest.mark.unit
def test_test_engine_dispatches_pyspark_to_docker(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``pyspark`` engine is probed inside the Docker image, not on the host."""
    from ovid2.web.services import engines_svc

    engines_svc.write_engine(
        "prod_pyspark", EngineWrite(type="pyspark", args={}, secrets={})
    )

    calls: list[list[str]] = []

    def _script(argv, **_kwargs):
        calls.append(list(argv))
        # First call is `docker image inspect ovid2:latest` → present.
        if argv[:3] == ["/usr/bin/docker", "image", "inspect"] or argv[:3] == [
            "docker",
            "image",
            "inspect",
        ]:
            return ("", "", 0)
        # Second call is the real probe.
        return (
            '{"ok": true, "code": "ok", "message": "Spark session built and '
            'responded to SELECT 1", "detail": {}}\n',
            "",
            0,
        )

    monkeypatch.setattr(engines_svc.shutil, "which", lambda _cmd: "/usr/bin/docker")
    monkeypatch.setattr(engines_svc.subprocess, "run", _fake_subprocess_run(_script))

    report = engines_svc.test_engine("prod_pyspark")

    assert report.ok is True
    assert report.code == "ok"
    # Two subprocess calls: image inspect, then the docker-run probe.
    assert len(calls) == 2
    probe_argv = calls[1]
    assert probe_argv[0] == "docker"
    assert probe_argv[1:3] == ["run", "--rm"]
    assert "ovid2:latest" in probe_argv
    # No stray "ovid2" argv token — the image ENTRYPOINT already runs it.
    assert probe_argv.count("ovid2") == 0
    assert probe_argv[-3:] == ["tools", "test-engine", "prod_pyspark"]
    # Read-only mount of the host engines directory.
    mount_flags = [a for a in probe_argv if a.startswith("/") and ":/etc/ovid2" in a]
    assert mount_flags, "expected a bind mount into /etc/ovid2"
    assert mount_flags[0].endswith(":ro")


@pytest.mark.unit
def test_test_engine_dispatches_snowpark_to_host(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``snowpark`` engine still probes in a host subprocess."""
    import sys as _sys

    from ovid2.web.models import SecretWrite
    from ovid2.web.services import engines_svc, secrets_svc

    # Snowpark requires an `account` + a secret; write the pieces first.
    secrets_svc.write_secret("engine_prod_snow", SecretWrite(values={"password": "x"}))
    engines_svc.write_engine(
        "prod_snow",
        EngineWrite(
            type="snowpark",
            args={"account": "acme", "user": "svc"},
            secrets={"password": "x"},
        ),
    )

    calls: list[list[str]] = []

    def _script(argv, **_kwargs):
        calls.append(list(argv))
        return (
            '{"ok": true, "code": "ok", "message": "Snowpark session responded", '
            '"detail": {}}\n',
            "",
            0,
        )

    monkeypatch.setattr(engines_svc.subprocess, "run", _fake_subprocess_run(_script))

    report = engines_svc.test_engine("prod_snow")

    assert report.ok is True
    assert len(calls) == 1
    argv = calls[0]
    # Host subprocess: sys.executable -m ovid2 tools test-engine <name>.
    assert argv[0] == _sys.executable
    assert argv[1:5] == ["-m", "ovid2", "tools", "test-engine"]
    assert argv[-1] == "prod_snow"
    # Absolutely no docker command in the host path.
    assert "docker" not in argv


@pytest.mark.unit
def test_test_engine_dispatches_databricks_to_host(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Databricks-Connect also probes on the host (no JVM needed)."""
    import sys as _sys

    from ovid2.web.models import SecretWrite
    from ovid2.web.services import engines_svc, secrets_svc

    secrets_svc.write_secret("engine_prod_dbx", SecretWrite(values={"token": "t"}))
    engines_svc.write_engine(
        "prod_dbx",
        EngineWrite(
            type="databricks_spark",
            args={"workspace_url": "https://dbx.example"},
            secrets={"token": "t"},
        ),
    )

    calls: list[list[str]] = []

    def _script(argv, **_kwargs):
        calls.append(list(argv))
        return ('{"ok": true, "code": "ok", "message": "OK", "detail": {}}\n', "", 0)

    monkeypatch.setattr(engines_svc.subprocess, "run", _fake_subprocess_run(_script))

    engines_svc.test_engine("prod_dbx")

    argv = calls[0]
    assert argv[0] == _sys.executable
    assert "docker" not in argv


@pytest.mark.unit
def test_test_engine_returns_no_image_when_image_missing_locally(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse to probe pyspark when the Docker image is not built.

    Cleaner than a py4j / entrypoint error the operator can't act on.
    """
    from ovid2.web.services import engines_svc

    engines_svc.write_engine(
        "prod_pyspark", EngineWrite(type="pyspark", args={}, secrets={})
    )

    ran: list[list[str]] = []

    def _script(argv, **_kwargs):
        ran.append(list(argv))
        # `docker image inspect` → not present.
        if "image" in argv and "inspect" in argv:
            return ("", "Error: No such image", 1)
        raise AssertionError(f"Should not have shelled out further; argv={argv!r}")

    monkeypatch.setattr(engines_svc.shutil, "which", lambda _cmd: "/usr/bin/docker")
    monkeypatch.setattr(engines_svc.subprocess, "run", _fake_subprocess_run(_script))

    report = engines_svc.test_engine("prod_pyspark")

    assert report.ok is False
    assert report.code == "no_image"
    assert "not built" in report.message
    assert "ovid2 image build" in report.detail.get("hint", "")
    # Exactly one subprocess call — the image inspect.
    assert len(ran) == 1


@pytest.mark.unit
def test_test_engine_returns_no_image_when_docker_missing(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same clean error when Docker itself is not installed on the host."""
    from ovid2.web.services import engines_svc

    engines_svc.write_engine(
        "prod_pyspark", EngineWrite(type="pyspark", args={}, secrets={})
    )

    def _script(argv, **_kwargs):  # pragma: no cover — should not be called
        raise AssertionError(f"subprocess.run should not run; argv={argv!r}")

    monkeypatch.setattr(engines_svc.shutil, "which", lambda _cmd: None)
    monkeypatch.setattr(engines_svc.subprocess, "run", _fake_subprocess_run(_script))

    report = engines_svc.test_engine("prod_pyspark")

    assert report.ok is False
    assert report.code == "no_image"
    assert "docker" in report.message.lower()


@pytest.mark.unit
def test_test_engine_bare_type_pyspark_dispatches_to_docker(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bare-type back-compat name ``pyspark`` also routes to Docker.

    Users who never write an engine JSON can still say
    ``--engine pyspark``; the Test button honours the same shortcut.
    """
    from ovid2.web.services import engines_svc

    calls: list[list[str]] = []

    def _script(argv, **_kwargs):
        calls.append(list(argv))
        if "image" in argv and "inspect" in argv:
            return ("", "", 0)
        return ('{"ok": true, "code": "ok", "message": "OK", "detail": {}}\n', "", 0)

    monkeypatch.setattr(engines_svc.shutil, "which", lambda _cmd: "/usr/bin/docker")
    monkeypatch.setattr(engines_svc.subprocess, "run", _fake_subprocess_run(_script))

    report = engines_svc.test_engine("pyspark")

    assert report.ok is True
    assert calls[-1][0] == "docker"


@pytest.mark.unit
def test_test_engine_unknown_falls_through_to_host(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown engine → host subprocess so the CLI reports the canonical error."""
    import sys as _sys

    from ovid2.web.services import engines_svc

    calls: list[list[str]] = []

    def _script(argv, **_kwargs):
        calls.append(list(argv))
        return (
            '{"ok": false, "code": "config", "message": "Engine config '
            '\'nope\'.json not found", "detail": {"exception_type": '
            '"FileNotFoundError"}}\n',
            "",
            1,
        )

    monkeypatch.setattr(engines_svc.subprocess, "run", _fake_subprocess_run(_script))

    report = engines_svc.test_engine("nope")

    assert report.ok is False
    assert report.code == "config"
    # Host argv, not docker.
    assert calls[0][0] == _sys.executable


@pytest.mark.unit
def test_test_engine_docker_argv_mount_uses_xdg_config_home(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bind mount source reflects ``$XDG_CONFIG_HOME`` at runtime."""
    from ovid2.web.services import engines_svc

    custom = isolated_xdg / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(custom))

    argv = engines_svc._pyspark_docker_argv("some_engine", "ovid2:1.2.3")

    mount_arg = argv[argv.index("-v") + 1]
    assert mount_arg == f"{custom}/ovid2:/etc/ovid2:ro"
    assert "ovid2:1.2.3" in argv


@pytest.mark.unit
def test_test_engine_timeout_returns_timeout_code(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Long-running probes are killed and surfaced as ``code=timeout``."""
    import subprocess as _subprocess

    from ovid2.web.services import engines_svc

    engines_svc.write_engine(
        "prod_pyspark", EngineWrite(type="pyspark", args={}, secrets={})
    )

    calls: list[list[str]] = []

    def _run(argv, **_kwargs):
        calls.append(list(argv))
        if "image" in argv and "inspect" in argv:
            return _subprocess.CompletedProcess(argv, 0, "", "")
        raise _subprocess.TimeoutExpired(cmd=argv, timeout=_kwargs.get("timeout", 0.0))

    monkeypatch.setattr(engines_svc.shutil, "which", lambda _cmd: "/usr/bin/docker")
    monkeypatch.setattr(engines_svc.subprocess, "run", _run)

    report = engines_svc.test_engine("prod_pyspark")

    assert report.ok is False
    assert report.code == "timeout"
    assert "argv" in report.detail
