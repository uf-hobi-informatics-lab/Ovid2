"""Unit tests for the break-glass local admin verifier (Todo 10)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ovid2.web.auth import breakglass


@pytest.fixture(autouse=True)
def _clean_lockout() -> None:
    """Reset the in-memory lockout state before every test."""
    breakglass._reset_lockout()


@pytest.mark.unit
def test_is_configured_false_before_set(isolated_xdg: Path) -> None:
    """No break-glass hash on disk means ``is_configured`` is False."""
    assert breakglass.is_configured() is False
    assert breakglass.verify("anything") is False


@pytest.mark.unit
def test_set_then_verify_true_for_correct_false_for_wrong(
    isolated_xdg: Path,
) -> None:
    """Setting a password makes the correct one verify and wrong ones fail."""
    breakglass.set_password("correct horse battery staple", actor="cli")
    assert breakglass.is_configured() is True
    assert breakglass.verify("correct horse battery staple") is True
    assert breakglass.verify("wrong password") is False


@pytest.mark.unit
def test_stored_hash_never_equals_plaintext(isolated_xdg: Path) -> None:
    """The persisted secret is an argon2 hash, never the plaintext."""
    plaintext = "s3cr3t-p@ssw0rd"
    breakglass.set_password(plaintext, actor="cli")
    path = (
        Path(os.environ["XDG_CONFIG_HOME"])
        / "ovid2"
        / "secrets.d"
        / f"{breakglass.SECRET_NAME}.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    stored = payload[breakglass.SECRET_KEY]
    assert stored != plaintext
    assert plaintext not in stored
    assert stored.startswith("$argon2")


@pytest.mark.unit
def test_lockout_blocks_correct_password_then_clears(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After MAX_FAILURES wrong tries, even the correct password is blocked."""
    fake_now = {"t": 1000.0}

    def _monotonic() -> float:
        return fake_now["t"]

    monkeypatch.setattr(breakglass.time, "monotonic", _monotonic)

    breakglass.set_password("correct", actor="cli")

    # Exhaust the failure budget.
    for _ in range(breakglass.MAX_FAILURES):
        assert breakglass.verify("wrong") is False

    # Now locked: the correct password is refused without being checked.
    assert breakglass.verify("correct") is False

    # Still locked just before the window elapses.
    fake_now["t"] = 1000.0 + breakglass.LOCKOUT_SECONDS - 1
    assert breakglass.verify("correct") is False

    # Advance past the lockout window: correct password works again.
    fake_now["t"] = 1000.0 + breakglass.LOCKOUT_SECONDS + 1
    assert breakglass.verify("correct") is True


@pytest.mark.unit
def test_correct_password_resets_failure_counter(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A success before the threshold clears accumulated failures."""
    monkeypatch.setattr(breakglass.time, "monotonic", lambda: 500.0)
    breakglass.set_password("correct", actor="cli")

    # A few wrong attempts, but below the threshold.
    for _ in range(breakglass.MAX_FAILURES - 1):
        assert breakglass.verify("wrong") is False

    # A correct login resets the counter.
    assert breakglass.verify("correct") is True

    # We can now fail (MAX_FAILURES - 1) more times without a lockout.
    for _ in range(breakglass.MAX_FAILURES - 1):
        assert breakglass.verify("wrong") is False
    # Still not locked — the correct password remains accepted.
    assert breakglass.verify("correct") is True


@pytest.mark.unit
def test_lockout_deadline_uses_full_window(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lockout deadline is exactly LOCKOUT_SECONDS from the last failure."""
    fake_now = {"t": 0.0}
    monkeypatch.setattr(breakglass.time, "monotonic", lambda: fake_now["t"])

    breakglass.set_password("correct", actor="cli")
    for _ in range(breakglass.MAX_FAILURES):
        assert breakglass.verify("wrong") is False

    # Just before the deadline the lock is still active (strict >).
    fake_now["t"] = float(breakglass.LOCKOUT_SECONDS) - 0.001
    assert breakglass.verify("correct") is False

    # At (and past) the deadline the lock has elapsed.
    fake_now["t"] = float(breakglass.LOCKOUT_SECONDS)
    assert breakglass.verify("correct") is True


@pytest.mark.unit
def test_set_password_rejects_empty(isolated_xdg: Path) -> None:
    """An empty password is refused before any hashing / write."""
    with pytest.raises(ValueError):
        breakglass.set_password("", actor="cli")
    assert breakglass.is_configured() is False


@pytest.mark.unit
def test_set_password_writes_audit_row(isolated_xdg: Path) -> None:
    """``set_password`` records a ``breakglass.set`` audit row."""
    from ovid2.web import db as web_db

    breakglass.set_password("pw", actor="alice")
    with web_db.connect() as conn:
        rows = conn.execute(
            "SELECT actor, action FROM web_audit WHERE action = ?",
            ("breakglass.set",),
        ).fetchall()
    assert any(r["actor"] == "alice" for r in rows)
