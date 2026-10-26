"""Storage-account folder-browse service (Phase 4).

Resolves a storage account (connection) name and lists one level of its
contents so the web panel can render a folder view. A driver that does
not implement ``list_dir`` surfaces as ``supported=False`` rather than a
500.

**Trust model (harness / API / executor):** the actual credential
resolution + ``list_dir`` call happen in a short-lived executor
subprocess (``ovid2 tools browse-storage <name>``), *not* in this
long-lived web process. This module only launches that subprocess and
maps its JSON result onto :class:`StorageBrowseResponse`, so the API
tier never holds a plaintext secret (enforced by
``tests/contract/test_web_no_plaintext_secret_reads.py``). See
the adr-harness-api-executor-trust-model design note.
"""

from __future__ import annotations

import logging
import os

from ovid2.config import connections as _connections
from ovid2.web.models import StorageBrowseEntry, StorageBrowseResponse
from ovid2.web.services._broker import BrokerError, run_tool_json

logger = logging.getLogger(__name__)


def _browse_deadline_seconds() -> float:
    """Return the wall-clock ceiling for a single browse call.

    Object-store / warehouse listings are cheap (``SHOW TABLES`` etc.)
    and finish in a second or two, but opening a large Microsoft Access
    ``.accdb`` over UCanAccess builds an on-disk HSQLDB mirror first —
    which for a multi-hundred-MB file can take a couple of minutes on
    the first open. The default therefore errs generous; deployments
    can tune it with ``$OVID2_BROWSE_DEADLINE_SECONDS``.
    """
    raw = os.environ.get("OVID2_BROWSE_DEADLINE_SECONDS", "")
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_BROWSE_DEADLINE_SECONDS
    return value if value > 0 else _DEFAULT_BROWSE_DEADLINE_SECONDS


#: Default wall-clock ceiling for a single browse call (seconds).
_DEFAULT_BROWSE_DEADLINE_SECONDS = 300.0

#: Back-compat alias (tests / callers may import this name).
BROWSE_DEADLINE_SECONDS = _DEFAULT_BROWSE_DEADLINE_SECONDS

__all__ = ["StorageBrowseError", "browse_storage"]


class StorageBrowseError(ValueError):
    """Raised when the storage account cannot be resolved."""


def browse_storage(name: str, prefix: str | None = None) -> StorageBrowseResponse:
    """List one level of a storage account's contents.

    Args:
        name: Storage-account (connection) config name.
        prefix: Sub-path / prefix to descend into. ``None`` = root.

    Returns:
        A :class:`StorageBrowseResponse`. ``supported=False`` when the
        driver has no ``list_dir`` capability.

    Raises:
        StorageBrowseError: When the connection does not exist.
    """
    # Existence check stays in the API tier (no secret needed) so a
    # missing account surfaces a clean error without spawning anything.
    if _connections.locate_connection(name) is None:
        raise StorageBrowseError(f"Storage account '{name}' not found.")

    tool_args = ["browse-storage", name]
    if prefix:
        tool_args += ["--prefix", prefix]

    try:
        result = run_tool_json(tool_args, timeout=_browse_deadline_seconds())
    except BrokerError as exc:
        logger.warning("Storage browse '%s' failed: %s", name, exc)
        return StorageBrowseResponse(
            storage=name,
            type="",
            prefix=prefix or "",
            supported=True,
            message=f"Listing failed: {exc}",
            entries=[],
        )

    entries = [
        StorageBrowseEntry(
            name=str(e.get("name", "")),
            kind=str(e.get("kind", "")),
            path=str(e.get("path", "")),
        )
        for e in (result.get("entries") or [])
    ]
    return StorageBrowseResponse(
        storage=name,
        type=str(result.get("type") or ""),
        prefix=prefix or "",
        supported=bool(result.get("supported", True)),
        message=result.get("message"),
        entries=entries,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("This module is imported by the ovid2 web routes; it is not a script.")
