"""Declarative route-policy markers read by the contract suite.

Every ``DELETE`` route under ``/api/v1`` must either route through the
human-approval gate (``_gate(...)`` in ``api_v1``) or declare itself
*owner-scoped* with :func:`owner_scoped` — a delete that only ever touches
the calling principal's own, non-shared object (a chat session, a private
note, a share the caller granted). The marker lands in the route's
OpenAPI ``x-ovid2-owner-scoped`` extension so it is visible in the API
docs and machine-checkable by
``tests/contract/test_destructive_ops_require_approval.py``.
"""

from __future__ import annotations

from typing import Any

__all__ = ["OWNER_SCOPED_KEY", "owner_scoped"]

#: OpenAPI extension key carrying the owner-scoped justification.
OWNER_SCOPED_KEY = "x-ovid2-owner-scoped"


def owner_scoped(reason: str) -> dict[str, Any]:
    """Return ``openapi_extra`` marking a route as an owner-scoped delete.

    Args:
        reason: One sentence explaining why the route needs no approval
            gate (what it deletes and why only the caller is affected).

    Returns:
        A dict to pass as the route decorator's ``openapi_extra``.

    Raises:
        ValueError: When ``reason`` is blank.
    """
    if not reason.strip():
        raise ValueError("owner_scoped() needs a non-empty reason.")
    return {OWNER_SCOPED_KEY: reason.strip()}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("This module is imported by the ovid2 web routes.")
