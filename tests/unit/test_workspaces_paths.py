"""Unit tests for the workspace path / secret / executor seams (adr-workspaces.md)."""

from __future__ import annotations

import os
from argparse import Namespace
from pathlib import Path

import pytest

from ovid2 import workspaces


@pytest.fixture()
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
        (tmp_path / var).mkdir()
        monkeypatch.setenv(var, str(tmp_path / var))
    for var in (
        "OVID2_WORKSPACE",
        "OVID2_CONNECTIONS_DIR",
        "OVID2_CONFIGS_DIR",
        "OVID2_ENGINES_DIR",
        "OVID2_LLMS_DIR",
        "OVID2_TRANSFORMS_DIR",
        "OVID2_PIPELINES_DIR",
        "OVID2_WORKFLOWS_DIR",
        "OVID2_SECRETS_DIR",
        "OVID2_WORKSPACE_PACKAGES_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    return tmp_path / "XDG_CONFIG_HOME"


@pytest.mark.unit
def test_active_workspace_resolution(monkeypatch: pytest.MonkeyPatch, xdg: Path) -> None:
    assert workspaces.active_workspace() == "default"
    monkeypatch.setenv("OVID2_WORKSPACE", "lab")
    assert workspaces.active_workspace() == "lab"
    with workspaces.use_workspace("other"):
        assert workspaces.active_workspace() == "other"
    assert workspaces.active_workspace() == "lab"
    monkeypatch.setenv("OVID2_WORKSPACE", "../etc")
    assert workspaces.active_workspace() == workspaces.NO_WORKSPACE


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["", "A", "a", "-x", "x/y", "new", "__none__", "x" * 41])
def test_validate_slug_rejects(bad: str) -> None:
    with pytest.raises(workspaces.WorkspaceSlugError):
        workspaces.validate_slug(bad)


@pytest.mark.unit
def test_default_is_the_flat_tree_others_are_isolated(xdg: Path) -> None:
    from ovid2.config import connections

    assert connections.connections_dir() == xdg / "ovid2" / "connections"
    with workspaces.use_workspace("lab"):
        assert connections.connections_dir() == xdg / "ovid2" / "workspaces" / "lab" / "connections"
        dirs = connections._search_paths()
        assert xdg / "ovid2" / "connections" not in dirs
        assert Path.cwd() / "connections" not in dirs
        assert dirs[-1] == Path("/etc/ovid2/connections")


@pytest.mark.unit
def test_connection_lookup_does_not_cross_workspaces(xdg: Path) -> None:
    from ovid2.config import connections

    (xdg / "ovid2" / "connections").mkdir(parents=True)
    (xdg / "ovid2" / "connections" / "shared_name.json").write_text("{}", encoding="utf-8")
    assert connections.locate_connection("shared_name") is not None
    with workspaces.use_workspace("lab"):
        assert connections.locate_connection("shared_name") is None
        assert "shared_name" not in connections.list_available_connections()


@pytest.mark.unit
def test_scoped_secret_sources_follow_the_workspace(xdg: Path) -> None:
    from ovid2.config.executor_env import secret_source_dirs, source_for

    default_secrets = xdg / "ovid2" / "secrets.d"
    default_secrets.mkdir(parents=True)
    (default_secrets / "conn_a.json").write_text("{}", encoding="utf-8")
    (default_secrets / "xor_seed.txt").write_text("seed", encoding="utf-8")
    (default_secrets / "auth_session.json").write_text("{}", encoding="utf-8")
    with workspaces.use_workspace("lab"):
        workspaces.materialise("lab", ("connections",))
        lab = workspaces.secrets_dir("lab")
        assert oct(lab.stat().st_mode & 0o777) == "0o700"
        sources = secret_source_dirs()
        assert sources == [lab]
        # Another workspace's secret is never granted…
        assert source_for("conn_a.json", sources) is None
        # …the site-wide auth secrets never reach an executor…
        assert source_for("auth_session.json", sources) is None
        # …but the shared XOR seed is.
        assert source_for("xor_seed.txt", sources) == default_secrets / "xor_seed.txt"


@pytest.mark.unit
def test_load_secret_reads_the_workspace_tree(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ovid2.config import secrets as secrets_mod

    monkeypatch.setenv("OVID2_SECRETS_SCOPE_MODE", "warn")
    workspaces.materialise("lab", ())
    path = workspaces.secrets_dir("lab") / "tok.json"
    path.write_text('{"k": "v"}', encoding="utf-8")
    os.chmod(path, 0o600)
    with workspaces.use_workspace("lab"):
        assert secrets_mod.load_secret("tok") == {"k": "v"}
    with pytest.raises(secrets_mod.SecretNotFoundError):
        secrets_mod.load_secret("tok")


@pytest.mark.unit
def test_stamp_workspace_sets_env(xdg: Path) -> None:
    from ovid2.config.executor_env import stamp_workspace

    with workspaces.use_workspace("lab"):
        env = stamp_workspace({"OVID2_WORKSPACE_PACKAGES_DIR": "/stale"})
    assert env["OVID2_WORKSPACE"] == "lab"
    assert env["OVID2_WORKSPACE_PACKAGES_DIR"].endswith("/workspaces/lab/packages")
    env = stamp_workspace({"OVID2_WORKSPACE_PACKAGES_DIR": "/stale"})
    assert env["OVID2_WORKSPACE"] == "default"
    assert "OVID2_WORKSPACE_PACKAGES_DIR" not in env


@pytest.mark.unit
def test_docker_argv_hides_other_workspaces(
    xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The driver container sees only its own workspace's sub-tree."""
    import ovid2.core.docker as docker_module
    from ovid2.core.isolation import OvidInstance

    monkeypatch.setattr(
        OvidInstance, "ensure_nss_files", lambda self, *a, **kw: (Path("/tmp/p"), Path("/tmp/g"))
    )
    monkeypatch.setattr(OvidInstance, "ensure_directories", lambda self: None)
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(docker_module, "DATA_DIR", data)
    monkeypatch.setattr(docker_module, "LEGACY_CREDENTIALS_DIR", tmp_path / "absent")
    monkeypatch.setenv("OVID2_SECRETS_SCOPE_MODE", "enforce")
    for slug in ("lab", "other"):
        workspaces.materialise(slug, ("connections",))
    args = Namespace(
        command="etl",
        input_address="in",
        output_address="out",
        pipeline="p",
        batch_id="b",
        tables=["t"],
        transformations=["format"],
        engine="pyspark",
    )
    with workspaces.use_workspace("lab"):
        cmd = docker_module.build_docker_command(
            args, OvidInstance(instance_id="20260101-000000-abcdef")
        )
    argv = " ".join(cmd)
    assert "OVID2_WORKSPACE=lab" in argv
    assert "/ovid2-user-config/ovid2/workspaces/other:ro,size=64k" in argv
    assert "/ovid2-user-config/ovid2/workspaces/lab:ro" not in argv
    # The workspace's own secrets.d is shadowed too (only the scoped grant is reachable).
    assert "/ovid2-user-config/ovid2/workspaces/lab/secrets.d:ro,size=64k" in argv
    assert (
        "PYTHONPATH=/app/src:/app/pipelines:/ovid2-user-config/ovid2/workspaces/lab/packages"
        in argv
    )


@pytest.mark.unit
def test_worker_env_carries_the_job_workspace(xdg: Path) -> None:
    from ovid2.web.models import JobDetail
    from ovid2.web.worker import Worker

    job = JobDetail(
        job_id="j",
        submitted_at="t",
        submitted_by="u",
        pipeline="p",
        batch_id="b",
        input_address="i",
        output_address="o",
        tables=[],
        transformations=[],
        status="running",
        instance_id="20260101-000000-abcdef",
        workspace_id="lab",
    )
    env = Worker()._build_env(job)
    assert env["OVID2_WORKSPACE"] == "lab"
    assert env["OVID2_WORKSPACE_PACKAGES_DIR"].endswith("workspaces/lab/packages")


@pytest.mark.unit
def test_state_key_namespaces_non_default(xdg: Path) -> None:
    from ovid2.web.db import state_key

    assert state_key("nightly") == "nightly"
    with workspaces.use_workspace("lab"):
        assert state_key("nightly") == "lab::nightly"


@pytest.mark.unit
def test_workspace_packages_are_visible_only_in_their_workspace(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from ovid2.config import pipeline_packages as pp

    workspaces.materialise("lab", ())
    root = workspaces.packages_dir("lab")
    assert root is not None
    pkg = root / "ovid2_pipelines" / "zz_ws_only_pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "path", list(sys.path))
    assert root in pp.ensure_all_workspace_packages_on_path()
    assert pp.package_workspace("zz_ws_only_pkg") == "lab"
    assert not pp.package_visible("zz_ws_only_pkg")
    with workspaces.use_workspace("lab"):
        assert pp.package_visible("zz_ws_only_pkg")
    # Shared packages stay visible everywhere.
    with workspaces.use_workspace("lab"):
        assert pp.package_visible("example_pcornet")


@pytest.mark.unit
def test_scaffold_refuses_a_name_used_by_another_workspace(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from ovid2.config import pipeline_packages as pp
    from ovid2.tools import new_pipeline

    workspaces.materialise("lab", ())
    root = workspaces.packages_dir("lab")
    assert root is not None
    (root / "ovid2_pipelines" / "zz_taken").mkdir(parents=True)
    monkeypatch.setattr(sys, "path", list(sys.path))
    pp.ensure_all_workspace_packages_on_path()
    with workspaces.use_workspace("other"):
        workspaces.materialise("other", ())
        with pytest.raises(new_pipeline.NewPipelineError, match="already used"):
            new_pipeline.scaffold_pipeline(
                "zz_taken", source_pipeline="example_pcornet", cdm="pcornet_7_0"
            )
