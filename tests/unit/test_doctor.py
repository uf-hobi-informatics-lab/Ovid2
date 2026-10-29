"""Unit tests for the ``ovid2 doctor`` diagnostic runner."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from ovid2.installer import doctor


@pytest.mark.unit
def test_python_version_check_reports_ok() -> None:
    """The Python-version check passes on the (3.11+) test interpreter."""
    result = doctor._check_python_version()
    assert result.severity == "ok"
    assert result.name == "python"


@pytest.mark.unit
@pytest.mark.parametrize("version", [(3, 9, 18), (3, 10, 22)])
def test_python_version_check_flags_old_interpreters(
    monkeypatch: pytest.MonkeyPatch, version: tuple[int, int, int]
) -> None:
    """Interpreters below the 3.11 floor must report 'error'."""
    import sys
    from typing import NamedTuple

    class _Ver(NamedTuple):
        major: int
        minor: int
        micro: int

    monkeypatch.setattr(sys, "version_info", _Ver(*version))
    result = doctor._check_python_version()
    assert result.severity == "error"
    assert f"{version[0]}.{version[1]}" in result.message


@pytest.mark.unit
def test_writable_dir_check_creates_and_reports_ok(tmp_path: Path) -> None:
    """``_check_writable_dir`` creates a missing directory and reports OK."""
    target = tmp_path / "subdir"
    result = doctor._check_writable_dir("test", target)
    assert result.severity == "ok"
    assert target.is_dir()


@pytest.mark.unit
def test_writable_dir_check_flags_read_only(tmp_path: Path) -> None:
    """A read-only directory should be flagged as an error."""
    target = tmp_path / "readonly"
    target.mkdir()
    target.chmod(0o500)
    try:
        result = doctor._check_writable_dir("test", target, create=False)
        assert result.severity == "error"
    finally:
        target.chmod(0o700)


@pytest.mark.unit
def test_secret_permissions_check_flags_world_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A world-readable secret file should be flagged as an error."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    secrets = tmp_path / "ovid2" / "secrets.d"
    secrets.mkdir(parents=True)
    bad = secrets / "leaky.json"
    bad.write_text("{}")
    bad.chmod(0o644)

    results = doctor._check_secrets_permissions()
    names = [r.name for r in results]
    assert f"secret:{bad.name}" in names
    entry = next(r for r in results if r.name == f"secret:{bad.name}")
    assert entry.severity == "error"


@pytest.mark.unit
def test_secret_permissions_check_accepts_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 0600 secret file should be reported as OK."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    secrets = tmp_path / "ovid2" / "secrets.d"
    secrets.mkdir(parents=True)
    good = secrets / "snowflake.json"
    good.write_text("{}")
    good.chmod(stat.S_IRUSR | stat.S_IWUSR)

    results = doctor._check_secrets_permissions()
    entry = next(r for r in results if r.name == f"secret:{good.name}")
    assert entry.severity == "ok"


@pytest.mark.unit
def test_run_all_checks_returns_report_and_all_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_all_checks` returns a non-empty list of named results."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))

    results = doctor.run_all_checks(image_ref="ovid2:none")
    assert results
    assert all(r.name and r.severity in {"ok", "warning", "error"} for r in results)

    text = doctor.format_report(results)
    assert "Ovid2 doctor report" in text


@pytest.mark.unit
def test_highest_severity_selects_worst() -> None:
    """`_highest_severity` should return the strongest present severity."""
    results = [
        doctor.CheckResult(name="a", severity="ok", message=""),
        doctor.CheckResult(name="b", severity="warning", message=""),
        doctor.CheckResult(name="c", severity="ok", message=""),
    ]
    assert doctor._highest_severity(results) == "warning"

    results.append(doctor.CheckResult(name="d", severity="error", message=""))
    assert doctor._highest_severity(results) == "error"
