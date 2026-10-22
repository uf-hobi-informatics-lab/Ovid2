"""Unit tests for ``ovid2 service`` template rendering."""

from __future__ import annotations

import pytest

from ovid2.installer import service


@pytest.mark.unit
def test_render_service_unit_substitutes_placeholders() -> None:
    """The user and executable path must land in the rendered unit file."""
    rendered = service.render_service_unit(user="ovid", ovid2_bin="/usr/local/bin/ovid2")
    assert "User=ovid" in rendered
    assert "ExecStart=/usr/local/bin/ovid2 scheduler" in rendered
    assert "@USER@" not in rendered
    assert "@OVID2_BIN@" not in rendered


@pytest.mark.unit
def test_render_tmpfiles_substitutes_user() -> None:
    """The tmpfiles.d template must substitute the target user."""
    rendered = service.render_tmpfiles(user="ovid")
    assert "@USER@" not in rendered
    assert "ovid" in rendered


@pytest.mark.unit
def test_render_logrotate_returns_config() -> None:
    """The logrotate template should mention the log dir."""
    rendered = service.render_logrotate()
    assert "/var/log/ovid2" in rendered


@pytest.mark.unit
def test_install_service_requires_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """`install_service` must refuse to run for a non-root uid."""
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    with pytest.raises(PermissionError):
        service.install_service(user="ovid", ovid2_bin="/usr/bin/ovid2")


@pytest.mark.unit
def test_uninstall_service_requires_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """`uninstall_service` must also gate on root."""
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    with pytest.raises(PermissionError):
        service.uninstall_service()
