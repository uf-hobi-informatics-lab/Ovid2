"""Unit tests for the install-wizard service (W5)."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest


@pytest.mark.unit
def test_compute_status_before_bootstrap(isolated_xdg: Path) -> None:
    """Every step reports ``pending`` (or ``done`` for derived state) pre-bootstrap."""
    from ovid2.web.services import install_svc

    status = install_svc.compute_status()
    keys = [s.key for s in status.steps]
    assert keys == list(install_svc.STEP_KEYS)
    assert status.current_step in {"bootstrap", "snowflake", "image", "service"}
    assert status.complete is False


@pytest.mark.unit
def test_run_bootstrap_creates_snowflake_template(isolated_xdg: Path) -> None:
    """``run_bootstrap`` drops the Snowflake template with 0o600."""
    from ovid2.web.services import install_svc

    log = install_svc.run_bootstrap(actor="unit")
    assert any("snowflake.json" in line for line in log)
    path = Path(os.environ["XDG_CONFIG_HOME"]) / "ovid2" / "secrets.d" / "snowflake.json"
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.unit
def test_bootstrap_step_flips_done_after_run(isolated_xdg: Path) -> None:
    """``bootstrap`` step transitions from pending to done."""
    from ovid2.web.services import install_svc

    before = next(s for s in install_svc.compute_status().steps if s.key == "bootstrap")
    install_svc.run_bootstrap(actor="unit")
    after = next(s for s in install_svc.compute_status().steps if s.key == "bootstrap")
    assert before.status in {"pending", "done"}
    assert after.status == "done"


@pytest.mark.unit
def test_snowflake_step_detects_placeholder(isolated_xdg: Path) -> None:
    """A freshly-bootstrapped snowflake.json is pending, not done."""
    from ovid2.web.services import install_svc

    install_svc.run_bootstrap(actor="unit")
    step = next(s for s in install_svc.compute_status().steps if s.key == "snowflake")
    assert step.status == "pending"
    assert "placeholder" in step.summary.lower()


@pytest.mark.unit
def test_save_snowflake_secret_writes_0600(isolated_xdg: Path) -> None:
    """Saving Snowflake credentials writes with mode 0600."""
    from ovid2.web.models import SecretWrite
    from ovid2.web.services import install_svc

    install_svc.run_bootstrap(actor="unit")
    payload = SecretWrite(
        values={
            "account": "acc",
            "user": "u",
            "password": "p",
            "role": "r",
            "warehouse": "w",
            "database": "d",
            "schema": "s",
        }
    )
    result = install_svc.save_snowflake_secret(payload, actor="unit")
    path = Path(result["path"])
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    step = next(s for s in install_svc.compute_status().steps if s.key == "snowflake")
    assert step.status == "done"


@pytest.mark.unit
def test_save_snowflake_missing_required_keys_raises(isolated_xdg: Path) -> None:
    """``save_snowflake_secret`` rejects payloads without ``password``."""
    from ovid2.web.models import SecretWrite
    from ovid2.web.services import install_svc

    install_svc.run_bootstrap(actor="unit")
    payload = SecretWrite(values={"account": "acc", "user": "u"})
    with pytest.raises(install_svc.InstallError):
        install_svc.save_snowflake_secret(payload, actor="unit")


@pytest.mark.unit
def test_snowflake_secret_never_returned_by_get_secret(isolated_xdg: Path) -> None:
    """The Snowflake password is never exposed by the secrets service."""
    from ovid2.web.models import SecretWrite
    from ovid2.web.services import install_svc, secrets_svc

    install_svc.run_bootstrap(actor="unit")
    payload = SecretWrite(
        values={
            "account": "a",
            "user": "u",
            "password": "hunter2",
            "role": "r",
            "warehouse": "w",
            "database": "d",
            "schema": "s",
        }
    )
    install_svc.save_snowflake_secret(payload, actor="unit")
    detail = secrets_svc.get_secret("snowflake")
    assert detail is not None
    assert "hunter2" not in detail.model_dump_json()


@pytest.mark.unit
def test_service_command_uses_provided_user_and_bin(isolated_xdg: Path) -> None:
    """``get_service_command`` composes the sudo invocation."""
    from ovid2.web.services import install_svc

    cmd = install_svc.get_service_command(user="alice", ovid2_bin="/opt/ovid2/bin/ovid2")
    assert cmd.user == "alice"
    assert cmd.ovid2_bin == "/opt/ovid2/bin/ovid2"
    assert "sudo ovid2 service install --user alice" in cmd.command
    assert "--bin /opt/ovid2/bin/ovid2" in cmd.command


@pytest.mark.unit
def test_verify_pipeline_returns_metadata(isolated_xdg: Path) -> None:
    """``verify_pipeline`` returns a summary for a discoverable package."""
    from ovid2.web.services import install_svc, pipelines_meta_svc

    pipelines = pipelines_meta_svc.discover_pipelines()
    if not pipelines:
        pytest.skip("No ovid2_pipelines packages on sys.path")
    meta = install_svc.verify_pipeline(pipelines[0])
    assert meta["name"] == pipelines[0]
    assert isinstance(meta["formatters"], int)
    assert isinstance(meta["mappers"], int)


@pytest.mark.unit
def test_verify_pipeline_missing_raises(isolated_xdg: Path) -> None:
    """``verify_pipeline`` raises on an unknown package."""
    from ovid2.web.services import install_svc

    with pytest.raises(install_svc.InstallError):
        install_svc.verify_pipeline("nonexistent_pipeline_xyz")


@pytest.mark.unit
def test_start_image_build_refuses_when_docker_missing(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``start_image_build`` errors out when docker is not on PATH."""
    from ovid2.web.services import install_svc

    monkeypatch.setattr(install_svc.shutil, "which", lambda _bin: None)
    with pytest.raises(install_svc.InstallError, match="docker"):
        install_svc.start_image_build(actor="unit")


@pytest.mark.unit
def test_start_image_build_refuses_missing_dockerfile(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``start_image_build`` errors when the Dockerfile path is missing."""
    from ovid2.web.services import install_svc

    monkeypatch.setattr(install_svc.shutil, "which", lambda _bin: "/usr/bin/docker")
    with pytest.raises(install_svc.InstallError, match="Dockerfile"):
        install_svc.start_image_build(
            context=str(tmp_path),
            dockerfile=str(tmp_path / "missing" / "Dockerfile"),
            actor="unit",
        )


@pytest.mark.unit
def test_start_image_build_records_row(
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A successful start inserts a ``web_image_builds`` row and returns detail."""
    from ovid2.web.services import install_svc

    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")

    # Point docker at /bin/true so the build "succeeds" immediately.
    monkeypatch.setattr(install_svc.shutil, "which", lambda _bin: "/bin/true")

    detail = install_svc.start_image_build(
        tag="test:latest",
        context=str(tmp_path),
        dockerfile=str(dockerfile),
        actor="unit",
    )
    assert detail.tag == "test:latest"
    assert detail.build_id
    # Wait a moment for the thread to update the row.
    for _ in range(50):
        again = install_svc.get_image_build(detail.build_id)
        if again is not None and again.status != install_svc.BUILD_RUNNING:
            break
        time.sleep(0.05)
    final = install_svc.get_image_build(detail.build_id)
    assert final is not None
    assert final.status in {install_svc.BUILD_SUCCEEDED, install_svc.BUILD_FAILED}


@pytest.mark.unit
def test_start_image_build_refuses_concurrent(
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A second submission is refused while another build is running."""
    from ovid2.web.services import install_svc

    monkeypatch.setattr(install_svc.shutil, "which", lambda _bin: "/usr/bin/docker")
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")

    # Directly insert a running row to simulate a live build.
    install_svc._ensure_image_builds_schema()
    from ovid2.web import db as web_db

    with web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_image_builds "
            "(build_id, submitted_at, submitted_by, tag, context, dockerfile, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "existing",
                install_svc._now_iso(),
                "unit",
                "test:latest",
                str(tmp_path),
                str(dockerfile),
                install_svc.BUILD_RUNNING,
            ),
        )
    with pytest.raises(install_svc.InstallError, match="already"):
        install_svc.start_image_build(
            tag="test:latest",
            context=str(tmp_path),
            dockerfile=str(dockerfile),
            actor="unit",
        )


@pytest.mark.unit
def test_list_image_builds_pagination(isolated_xdg: Path, tmp_path: Path) -> None:
    """``list_image_builds`` returns a paginated response envelope."""
    from ovid2.web import db as web_db
    from ovid2.web.services import install_svc

    install_svc._ensure_image_builds_schema()
    ctx = str(tmp_path)
    df = str(tmp_path / "Dockerfile")
    with web_db.connect() as conn:
        for i in range(3):
            conn.execute(
                "INSERT INTO web_image_builds "
                "(build_id, submitted_at, submitted_by, tag, context, dockerfile, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    f"b{i}",
                    install_svc._now_iso(),
                    "unit",
                    "test:latest",
                    ctx,
                    df,
                    install_svc.BUILD_SUCCEEDED,
                ),
            )
    page = install_svc.list_image_builds(limit=2, offset=0)
    assert page.total == 3
    assert len(page.builds) == 2


@pytest.mark.unit
def test_bootstrap_writes_audit_row(isolated_xdg: Path) -> None:
    """``run_bootstrap`` appends a ``install.bootstrap`` audit row."""
    from ovid2.web import db as web_db
    from ovid2.web.services import install_svc

    install_svc.run_bootstrap(actor="unit")
    with web_db.connect() as conn:
        rows = conn.execute(
            "SELECT action, actor FROM web_audit WHERE action = ?",
            ("install.bootstrap",),
        ).fetchall()
    assert len(rows) >= 1
    assert rows[0]["actor"] == "unit"
