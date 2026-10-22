"""Session store service (auth).

Owns the write path for web session records in the SQLite database.
Enforces session validity, expiry, and revocation logic.

Safety rules enforced here:

1. All SQL is parameterized — no f-string SQL.
2. Timestamps stored as ISO strings: datetime.now(timezone.utc).isoformat().
3. Compares by parsing with datetime.fromisoformat(...).
4. Absolute expiry always wins over idle expiry in touch().
5. Revoked sessions are ignored by get_valid().
6. Purge removes both expired and revoked rows.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from ovid2.web import db as web_db
from ovid2.web.models import SessionRecord

logger = __import__("logging").getLogger(__name__)

__all__ = [
    "create",
    "get_valid",
    "purge_expired",
    "revoke",
    "revoke_by_idp_sid",
    "touch",
]


def _now_iso() -> str:
    """Return current UTC time as ISO string."""
    return datetime.now(timezone.utc).isoformat()


def create(
    user_id: str,
    idp_sid: str | None,
    absolute_ttl_min: int,
    idle_ttl_min: int,
) -> SessionRecord:
    """Create a new web session.

    Args:
        user_id: Authenticated user identifier.
        idp_sid: Identity provider session ID for logout correlation.
        absolute_ttl_min: Absolute TTL in minutes (hard cap).
        idle_ttl_min: Idle TTL in minutes (sliding window).

    Returns:
        SessionRecord: The created session record.

    Note:
        session_id = secrets.token_urlsafe(32) (256-bit).
        now = utcnow. absolute_deadline = now + absolute_ttl_min minutes.
        idle_deadline = now + idle_ttl_min minutes.
        expires_at = min(absolute_deadline, idle_deadline).
        INSERT row with created_at=last_seen_at=now, expires_at,
        absolute_deadline, idp_sid, revoked=0.
    """
    session_id = secrets.token_urlsafe(32)
    now = _now_iso()
    absolute_deadline = (
        datetime.fromisoformat(now) + timedelta(minutes=absolute_ttl_min)
    ).isoformat()
    idle_deadline = (datetime.fromisoformat(now) + timedelta(minutes=idle_ttl_min)).isoformat()
    # expires_at is the earlier of absolute_deadline and idle_deadline
    expires_at = absolute_deadline if absolute_deadline <= idle_deadline else idle_deadline

    with web_db.connect() as conn:
        conn.execute(
            """
            INSERT INTO web_sessions (
                session_id, user_id, created_at, last_seen_at, expires_at,
                absolute_deadline, idp_sid, revoked
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                session_id,
                user_id,
                now,
                now,
                expires_at,
                absolute_deadline,
                idp_sid,
            ),
        )
        conn.commit()

    return SessionRecord(
        session_id=session_id,
        user_id=user_id,
        created_at=now,
        last_seen_at=now,
        expires_at=expires_at,
        absolute_deadline=absolute_deadline,
        idp_sid=idp_sid,
        revoked=False,
    )


def touch(session_id: str, idle_ttl_min: int) -> SessionRecord | None:
    """Slide idle expiry: update last_seen_at and expires_at.

    Args:
        session_id: The session to refresh.
        idle_ttl_min: Idle TTL in minutes for sliding window.

    Returns:
        SessionRecord | None: Updated record if session exists and is valid
        (not revoked and not past absolute_deadline), else None.

    Note:
        new_expires = min(now + idle_ttl_min, absolute_deadline).
        Update last_seen_at=now, expires_at=new_expires.
        Absolute cap ALWAYS wins.
    """
    now = _now_iso()
    with web_db.connect() as conn:
        row = conn.execute(
            """
            SELECT session_id, user_id, created_at, last_seen_at, expires_at,
                   absolute_deadline, idp_sid, revoked
            FROM web_sessions
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if not row:
            return None
        # Check if revoked or past absolute_deadline
        if row["revoked"] == 1:
            return None
        if datetime.fromisoformat(row["absolute_deadline"]) < datetime.fromisoformat(now):
            return None
        # Compute new expiry: min(now + idle_ttl_min, absolute_deadline)
        idle_deadline = (datetime.fromisoformat(now) + timedelta(minutes=idle_ttl_min)).isoformat()
        new_expires = (
            idle_deadline if idle_deadline <= row["absolute_deadline"] else row["absolute_deadline"]
        )
        conn.execute(
            """
            UPDATE web_sessions
            SET last_seen_at = ?, expires_at = ?
            WHERE session_id = ?
            """,
            (now, new_expires, session_id),
        )
        conn.commit()
        # Return updated record
        return SessionRecord(
            session_id=row["session_id"],
            user_id=row["user_id"],
            created_at=row["created_at"],
            last_seen_at=now,
            expires_at=new_expires,
            absolute_deadline=row["absolute_deadline"],
            idp_sid=row["idp_sid"],
            revoked=bool(row["revoked"]),
        )


def get_valid(session_id: str) -> SessionRecord | None:
    """Return session record if valid (not revoked and not expired).

    Args:
        session_id: The session to validate.

    Returns:
        SessionRecord | None: The record if revoked==0 AND now < expires_at,
        otherwise None.
    """
    now = _now_iso()
    with web_db.connect() as conn:
        row = conn.execute(
            """
            SELECT session_id, user_id, created_at, last_seen_at, expires_at,
                   absolute_deadline, idp_sid, revoked
            FROM web_sessions
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if not row:
            return None
        if row["revoked"] == 1:
            return None
        if datetime.fromisoformat(row["expires_at"]) <= datetime.fromisoformat(now):
            return None
        return SessionRecord(
            session_id=row["session_id"],
            user_id=row["user_id"],
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
            expires_at=row["expires_at"],
            absolute_deadline=row["absolute_deadline"],
            idp_sid=row["idp_sid"],
            revoked=bool(row["revoked"]),
        )


def revoke(session_id: str) -> None:
    """Revoke a session by setting revoked=1.

    Args:
        session_id: The session to revoke.
    """
    with web_db.connect() as conn:
        conn.execute(
            """
            UPDATE web_sessions
            SET revoked = 1
            WHERE session_id = ?
            """,
            (session_id,),
        )
        conn.commit()


def revoke_by_idp_sid(issuer: str, idp_sid: str) -> int:
    """Revoke all sessions matching idp_sid.

    Args:
        issuer: Identity provider issuer (accepted for signature/future-proofing).
        idp_sid: Identity provider session ID to match.

    Returns:
        int: Number of sessions revoked.
    """
    with web_db.connect() as conn:
        cursor = conn.execute(
            """
            UPDATE web_sessions
            SET revoked = 1
            WHERE idp_sid = ?
            """,
            (idp_sid,),
        )
        conn.commit()
        return cursor.rowcount


def purge_expired() -> int:
    """Delete rows where revoked==1 OR now >= expires_at.

    Returns:
        int: Number of rows deleted.
    """
    now = _now_iso()
    with web_db.connect() as conn:
        cursor = conn.execute(
            """
            DELETE FROM web_sessions
            WHERE revoked = 1 OR expires_at <= ?
            """,
            (now,),
        )
        conn.commit()
        return cursor.rowcount
