"""Unit tests for the W4 secret editor service."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from ovid2.web.models import SecretWrite
from ovid2.web.services import secrets_svc


@pytest.mark.unit
def test_create_writes_0600(isolated_xdg: Path) -> None:
    """A newly-created secret has mode 0600 on disk."""
    result = secrets_svc.write_secret(
        "test",
        SecretWrite(values={"user": "u", "password": "p"}),
        create_only=True,
    )
    path = Path(result.path)
    assert path.is_file()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {mode:04o}"
    payload = json.loads(path.read_text())
    assert payload == {"user": "u", "password": "p"}


@pytest.mark.unit
def test_list_never_returns_values(isolated_xdg: Path) -> None:
    """``list_secrets`` returns keys but never values."""
    secrets_svc.write_secret(
        "sf",
        SecretWrite(values={"user": "u", "password": "hunter2"}),
    )
    summaries = secrets_svc.list_secrets()
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.name == "sf"
    assert set(summary.keys) == {"user", "password"}
    # The Pydantic model doesn't expose values. Serialise defensively.
    dumped = summary.model_dump()
    assert "hunter2" not in json.dumps(dumped)
    assert "values" not in dumped


@pytest.mark.unit
def test_get_returns_keys_only(isolated_xdg: Path) -> None:
    """``get_secret`` returns key names but never values."""
    secrets_svc.write_secret(
        "sf", SecretWrite(values={"user": "u", "password": "hunter2"})
    )
    detail = secrets_svc.get_secret("sf")
    assert detail is not None
    assert set(detail.keys) == {"user", "password"}
    assert "hunter2" not in json.dumps(detail.model_dump())


@pytest.mark.unit
def test_update_preserves_masked(isolated_xdg: Path) -> None:
    """A ``***`` value keeps the existing on-disk secret value."""
    secrets_svc.write_secret(
        "sf",
        SecretWrite(values={"user": "u", "password": "real"}),
    )
    # Simulate UI edit: only touch 'user'; leave password sentinel.
    secrets_svc.write_secret(
        "sf",
        SecretWrite(values={"user": "u2", "password": "***"}),
    )
    raw = json.loads(
        (Path(os.environ["XDG_CONFIG_HOME"]) / "ovid2" / "secrets.d" / "sf.json")
        .read_text()
    )
    assert raw == {"user": "u2", "password": "real"}


@pytest.mark.unit
def test_refuses_permissive_overwrite(isolated_xdg: Path) -> None:
    """A pre-existing 0644 file cannot be overwritten via the editor."""
    secrets_dir = Path(os.environ["XDG_CONFIG_HOME"]) / "ovid2" / "secrets.d"
    secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = secrets_dir / "loose.json"
    path.write_text(json.dumps({"user": "u"}), encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(secrets_svc.SecretError, match="0644"):
        secrets_svc.write_secret(
            "loose", SecretWrite(values={"user": "n"})
        )


@pytest.mark.unit
def test_delete_removes_file(isolated_xdg: Path) -> None:
    """Delete unlinks the file."""
    secrets_svc.write_secret("bye", SecretWrite(values={"user": "u"}))
    result = secrets_svc.delete_secret("bye")
    assert result.action == "deleted"
    assert secrets_svc.get_secret("bye") is None


@pytest.mark.unit
def test_rejects_traversal_name(isolated_xdg: Path) -> None:
    """Path-traversal-ish names are rejected."""
    for bad in ("../evil", "with/slash", " space", "", ".hidden"):
        with pytest.raises(secrets_svc.SecretError):
            secrets_svc.write_secret(bad, SecretWrite(values={"k": "v"}))


@pytest.mark.unit
def test_rejects_non_scalar_value(isolated_xdg: Path) -> None:
    """Nested containers are rejected — only JSON scalars allowed."""
    with pytest.raises(secrets_svc.SecretError):
        secrets_svc.write_secret(
            "nested", SecretWrite(values={"a": {"nested": True}})
        )


@pytest.mark.unit
def test_empty_values_rejected(isolated_xdg: Path) -> None:
    """An empty ``values`` dict is refused."""
    with pytest.raises(secrets_svc.SecretError):
        secrets_svc.write_secret("empty", SecretWrite(values={}))


@pytest.mark.unit
def test_audit_row_written(isolated_xdg: Path) -> None:
    """Every mutation writes a ``web_audit`` row with hashes."""
    from ovid2.web import db as web_db

    secrets_svc.write_secret("audited", SecretWrite(values={"k": "v"}))
    with web_db.connect() as conn:
        rows = conn.execute(
            "SELECT action, target, after_hash FROM web_audit WHERE target = ?",
            ("audited",),
        ).fetchall()
    assert any(r["action"] == "secret.created" for r in rows)
    assert all(r["after_hash"] for r in rows if r["action"] == "secret.created")
