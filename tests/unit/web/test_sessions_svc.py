"""Unit tests for sessions_svc."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ovid2.web import db as web_db
from ovid2.web.services import sessions_svc


@pytest.fixture(autouse=True)
def _temp_db(isolated_xdg):
    """Ensure each test runs against a clean, temp DB."""
    web_db.ensure_schema()
    yield
    # Clean up after test
    with web_db.connect() as conn:
        conn.execute("DELETE FROM web_sessions")
        conn.commit()


def test_create_returns_session_record():
    """create() returns a SessionRecord with expected fields."""
    rec = sessions_svc.create(
        user_id="user123",
        idp_sid="idp_session_abc",
        absolute_ttl_min=60,
        idle_ttl_min=15,
    )
    assert rec.session_id  # token_urlsafe(32) -> 43 chars
    assert len(rec.session_id) == 43
    assert rec.user_id == "user123"
    assert rec.idp_sid == "idp_session_abc"
    assert rec.revoked is False
    # Timestamps should be recent
    now = datetime.now(timezone.utc)
    created = datetime.fromisoformat(rec.created_at)
    assert abs((now - created).total_seconds()) < 5
    # expires_at should be min(abs, idle)
    abs_dt = datetime.fromisoformat(rec.absolute_deadline)
    idle_dt = datetime.fromisoformat(rec.expires_at)
    # With absolute=60, idle=15 -> expires should be idle (15 min)
    assert abs_dt > idle_dt  # absolute is later
    assert idle_dt == datetime.fromisoformat(rec.expires_at)


def test_get_valid_returns_none_for_missing():
    """get_valid() returns None for unknown session_id."""
    assert sessions_svc.get_valid("unknown-session-id") is None


def test_get_valid_returns_session_when_fresh():
    """get_valid() returns session when created with generous TTLs."""
    rec = sessions_svc.create(
        user_id="user123",
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=60,
    )
    got = sessions_svc.get_valid(rec.session_id)
    assert got is not None
    assert got.session_id == rec.session_id
    assert got.user_id == "user123"


def test_get_valid_returns_none_when_idle_expired_immediately():
    """get_valid() returns None when idle_ttl_min=0 (idle-expired immediately)."""
    rec = sessions_svc.create(
        user_id="user123",
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=0,  # idle expires now
    )
    got = sessions_svc.get_valid(rec.session_id)
    assert got is None  # should be idle-expired


def test_get_valid_returns_none_when_absolutely_expired():
    """get_valid() returns None when absolute_ttl_min=0."""
    rec = sessions_svc.create(
        user_id="user123",
        idp_sid=None,
        absolute_ttl_min=0,  # absolutely expired now
        idle_ttl_min=60,
    )
    got = sessions_svc.get_valid(rec.session_id)
    assert got is None  # should be absolutely expired


def test_touch_slides_idle_expiry_within_window():
    """touch() updates last_seen_at and slides idle expiry."""
    # Create session with 10 min absolute, 5 min idle
    rec = sessions_svc.create(
        user_id="user123",
        idp_sid=None,
        absolute_ttl_min=10,
        idle_ttl_min=5,
    )
    # Touch immediately should slide idle expiry
    updated = sessions_svc.touch(rec.session_id, idle_ttl_min=5)
    assert updated is not None
    assert updated.last_seen_at != rec.last_seen_at  # updated
    # expires_at should be now + 5 min (idle), but capped by absolute (10 min)
    # Since we touched immediately, idle window is still 5 min from now
    # But absolute is 10 min from creation, which is > 5 min from now
    # So expires should be now + 5 min
    now = datetime.fromisoformat(updated.last_seen_at)
    expires = datetime.fromisoformat(updated.expires_at)
    absolute = datetime.fromisoformat(updated.absolute_deadline)
    # Should be close to 5 minute idle window
    assert abs((expires - now).total_seconds() - 5*60) < 5
    # Should not exceed absolute deadline
    assert expires <= absolute


def test_touch_cannot_exceed_absolute_deadline():
    """touch() cannot push expires_at beyond absolute_deadline."""
    # Create with short absolute (1 min), long idle (100 min)
    rec = sessions_svc.create(
        user_id="user123",
        idp_sid=None,
        absolute_ttl_min=1,
        idle_ttl_min=100,
    )
    # Touch immediately
    updated = sessions_svc.touch(rec.session_id, idle_ttl_min=100)
    assert updated is not None
    # expires_at should equal absolute_deadline (the cap)
    assert updated.expires_at == updated.absolute_deadline


def test_touch_returns_none_if_revoked():
    """touch() returns None if session is revoked."""
    rec = sessions_svc.create(
        user_id="user123",
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=60,
    )
    sessions_svc.revoke(rec.session_id)
    updated = sessions_svc.touch(rec.session_id, idle_ttl_min=60)
    assert updated is None


def test_touch_returns_none_if_past_absolute_deadline():
    """touch() returns None if session is past absolute_deadline."""
    # Create session that's already absolutely expired
    rec = sessions_svc.create(
        user_id="user123",
        idp_sid=None,
        absolute_ttl_min=0,  # expired now
        idle_ttl_min=60,
    )
    updated = sessions_svc.touch(rec.session_id, idle_ttl_min=60)
    assert updated is None


def test_revoke_makes_get_valid_return_none():
    """revoke() causes get_valid() to return None immediately."""
    rec = sessions_svc.create(
        user_id="user123",
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=60,
    )
    assert sessions_svc.get_valid(rec.session_id) is not None
    sessions_svc.revoke(rec.session_id)
    assert sessions_svc.get_valid(rec.session_id) is None


def test_revoke_by_idp_sid_matches_only_idp_sid():
    """revoke_by_idp_sid() revokes only matching sessions."""
    # Create two sessions: one with idp_sid='abc', one with None
    rec1 = sessions_svc.create(
        user_id="user1",
        idp_sid="abc",
        absolute_ttl_min=60,
        idle_ttl_min=60,
    )
    rec2 = sessions_svc.create(
        user_id="user2",
        idp_sid="def",
        absolute_ttl_min=60,
        idle_ttl_min=60,
    )
    rec3 = sessions_svc.create(
        user_id="user3",
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=60,
    )
    # Revoke idp_sid='abc'
    count = sessions_svc.revoke_by_idp_sid("issuer", "abc")
    assert count == 1  # only revoked rec1
    # Check states
    assert sessions_svc.get_valid(rec1.session_id) is None  # revoked
    assert sessions_svc.get_valid(rec2.session_id) is not None  # still valid
    assert sessions_svc.get_valid(rec3.session_id) is not None  # still valid


def test_purge_expired_removes_expired_and_revoked():
    """purge_expired() removes expired and revoked rows."""
    # Create mix: fresh, idle-expired, absolutely-expired, revoked
    fresh = sessions_svc.create(
        user_id="fresh",
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=60,
    )
    idle_exp = sessions_svc.create(
        user_id="idle_exp",
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=0,  # idle-expired now
    )
    abs_exp = sessions_svc.create(
        user_id="abs_exp",
        idp_sid=None,
        absolute_ttl_min=0,  # absolutely expired now
        idle_ttl_min=60,
    )
    revoked = sessions_svc.create(
        user_id="revoked",
        idp_sid=None,
        absolute_ttl_min=60,
        idle_ttl_min=60,
    )
    sessions_svc.revoke(revoked.session_id)

    # Run purge
    deleted = sessions_svc.purge_expired()
    assert deleted == 3  # idle_exp, abs_exp, revoked

    # Check remaining
    assert sessions_svc.get_valid(fresh.session_id) is not None
    assert sessions_svc.get_valid(idle_exp.session_id) is None
    assert sessions_svc.get_valid(abs_exp.session_id) is None
    assert sessions_svc.get_valid(revoked.session_id) is None
