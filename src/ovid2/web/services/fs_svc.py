"""Host-side directory browser for the connection form path picker.

The picker powers the ``disc.host_path`` widget on the connection
edit page. It runs against the web tier's *local* filesystem — the
web tier is host-managed via systemd (see
``resources/systemd/ovid2-web.service.in``), not containerised — so
``os.scandir`` reflects what the operator actually sees when they
SSH into the box.

Rules enforced by this module:

- **Read-only.** No writes, no listdir side effects beyond the
  bookkeeping ``os.scandir`` performs.
- **Allowlist-guarded.** Every request path is resolved with
  ``os.path.realpath`` and rejected unless it (or one of its
  ancestors) matches an entry in ``settings.fs_picker.roots``. The
  default allowlist is ``["/"]`` which is permissive; deployments
  can pin it to ``["/data", "/processing"]`` (or similar) via the
  settings JSON.
- **Kill-switch.** ``settings.fs_picker.enabled = False`` makes
  every function raise :class:`FsPickerDisabledError`; the routes
  translate that into 404 so a disabled picker is indistinguishable
  from an absent one from the outside.
- **Directory-only by default.** :func:`list_dir` returns entries
  of every kind (files + dirs) so the UI can decide what to render;
  the connection picker widget filters to directories client-side.

Errors surface via a small exception hierarchy so the routes can
translate each condition into the right HTTP status code without
re-implementing the classification.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ovid2.web.models import FsEntry, FsListing, FsRoot, FsRoots
from ovid2.web.services import settings_svc

logger = logging.getLogger(__name__)

__all__ = [
    "FsAccessError",
    "FsNotFoundError",
    "FsPickerDisabledError",
    "FsPickerError",
    "list_dir",
    "list_roots",
    "normalise_allowlist",
]


class FsPickerError(ValueError):
    """Base class for every picker failure. Never raised directly."""


class FsPickerDisabledError(FsPickerError):
    """Raised when ``settings.fs_picker.enabled`` is False."""


class FsAccessError(FsPickerError):
    """Raised when a path is outside the allowlist or lacks OS permission."""


class FsNotFoundError(FsPickerError):
    """Raised when a resolved path does not exist."""


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


def normalise_allowlist(roots: list[str]) -> list[str]:
    """Return the allowlist as canonical, absolute, real paths.

    - Empty / non-absolute entries are dropped.
    - Trailing slashes are stripped (``os.path.normpath`` behavior).
    - The list is de-duplicated while preserving order.
    - When the caller supplied nothing (or every entry got dropped)
      we fall back to ``["/"]`` so the feature stays usable rather
      than silently locking every path out.
    """
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in roots:
        if not isinstance(raw, str):
            continue
        candidate = raw.strip()
        if not candidate or not os.path.isabs(candidate):
            continue
        # Do not follow symlinks when normalising the allowlist —
        # operators expect ``/processing`` in the JSON to mean
        # exactly ``/processing``, not whatever it happens to point
        # at today.
        canonical = os.path.normpath(candidate)
        if canonical in seen:
            continue
        seen.add(canonical)
        cleaned.append(canonical)
    return cleaned or ["/"]


def _path_within(target: str, root: str) -> bool:
    """Return True when ``target`` sits at ``root`` or beneath it."""
    if root == "/":
        return True
    # Add a trailing separator so /foo does not "match" /foobar.
    root_prefix = root.rstrip("/") + "/"
    return target == root or target.startswith(root_prefix)


def _check_allowlist(path: str, allowlist: list[str]) -> None:
    """Raise :class:`FsAccessError` when ``path`` sits outside ``allowlist``."""
    real = os.path.realpath(path)
    canonical = os.path.normpath(real)
    for root in allowlist:
        if _path_within(canonical, root):
            return
    raise FsAccessError(
        f"Path {path!r} is outside the fs_picker allowlist "
        f"(roots={allowlist})."
    )


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def _entry_type(entry: os.DirEntry[str]) -> str:
    """Classify a ``DirEntry`` into one of four buckets."""
    try:
        if entry.is_symlink():
            # Follow the link once to decide dir-vs-file — but ignore
            # broken symlinks (report them as 'symlink' so the picker
            # can grey them out client-side).
            try:
                if entry.is_dir():
                    return "dir"
                if entry.is_file():
                    return "file"
            except OSError:
                return "symlink"
            return "symlink"
        if entry.is_dir(follow_symlinks=False):
            return "dir"
        if entry.is_file(follow_symlinks=False):
            return "file"
    except OSError:
        return "other"
    return "other"


def list_dir(
    path: str | None,
    *,
    show_hidden: bool | None = None,
) -> FsListing:
    """List the entries of a directory on the host filesystem.

    Args:
        path: Absolute path to list. ``None`` / empty string falls
            back to the first allowlist root (``/`` when unset).
        show_hidden: When ``None`` the settings default applies.
            Otherwise the value overrides the default for this
            call.

    Returns:
        A :class:`~ovid2.web.models.FsListing`.

    Raises:
        FsPickerDisabledError: When the feature is disabled globally.
        FsAccessError: When the resolved path is outside the
            allowlist or the process lacks read permission.
        FsNotFoundError: When the resolved path does not exist.
    """
    settings = settings_svc.get_settings()
    picker = settings.fs_picker
    if not picker.enabled:
        raise FsPickerDisabledError("Filesystem picker is disabled.")
    allowlist = normalise_allowlist(list(picker.roots))
    hidden = picker.show_hidden if show_hidden is None else bool(show_hidden)

    target = (path or "").strip() or allowlist[0]
    if not os.path.isabs(target):
        raise FsAccessError(f"Path must be absolute, got {target!r}.")
    _check_allowlist(target, allowlist)

    if not os.path.exists(target):
        raise FsNotFoundError(f"Path {target!r} does not exist.")
    if not os.path.isdir(target):
        raise FsAccessError(f"Path {target!r} is not a directory.")
    if not os.access(target, os.R_OK | os.X_OK):
        raise FsAccessError(f"No read permission on {target!r}.")

    entries: list[FsEntry] = []
    try:
        with os.scandir(target) as scanner:
            for entry in scanner:
                if not hidden and entry.name.startswith("."):
                    continue
                entries.append(
                    FsEntry(
                        name=entry.name,
                        type=_entry_type(entry),
                        is_symlink=entry.is_symlink(),
                    )
                )
    except PermissionError as exc:
        raise FsAccessError(str(exc)) from exc
    except OSError as exc:
        raise FsAccessError(f"Cannot list {target!r}: {exc}") from exc

    # Directories first, then case-insensitive alphabetical within
    # each kind. Keeps the picker stable regardless of the on-disk
    # order the OS happens to hand us.
    entries.sort(key=lambda e: (0 if e.type == "dir" else 1, e.name.lower()))

    parent = str(Path(target).parent) if target not in allowlist and target != "/" else None
    if parent is not None:
        # Never surface a parent that sits outside the allowlist —
        # the "Up" button becomes a no-op instead of a policy hole.
        try:
            _check_allowlist(parent, allowlist)
        except FsAccessError:
            parent = None

    return FsListing(
        path=target,
        parent=parent,
        entries=entries,
        show_hidden=hidden,
        allowed=True,
    )


# ---------------------------------------------------------------------------
# Roots
# ---------------------------------------------------------------------------


def _connection_roots() -> list[FsRoot]:
    """Return one :class:`FsRoot` per existing disc connection.

    Lets the operator jump straight to another disc's ``host_path``
    when they are creating a new connection that lives on the same
    tree. Silently skipped when the connections module raises — the
    picker keeps working even if a JSON is malformed.
    """
    roots: list[FsRoot] = []
    try:
        from ovid2.config import connections as _connections
        from ovid2.drivers.disc import normalise_disc_args
    except Exception:  # pragma: no cover — defensive
        return roots
    for name in _connections.list_available_connections():
        try:
            cfg = _connections.load_connection_config(name)
        except (FileNotFoundError, ValueError):
            continue
        if str(cfg.get("type") or "") != "disc":
            continue
        args = normalise_disc_args(dict(cfg.get("args") or {}))
        host = args["host_path"]
        if host and os.path.isabs(host):
            roots.append(
                FsRoot(label=f"disc: {name}", path=host, source="connection")
            )
    return roots


def list_roots() -> FsRoots:
    """Return the curated starting points offered by the picker.

    The list is: ``$HOME`` (when readable), every entry in
    ``settings.fs_picker.roots``, every disc-connection host_path,
    and ``/`` as a last-resort catch-all. Duplicates are removed
    while preserving order.
    """
    settings = settings_svc.get_settings()
    picker = settings.fs_picker
    if not picker.enabled:
        return FsRoots(roots=[], enabled=False)

    entries: list[FsRoot] = []
    seen: set[str] = set()

    def _add(root: FsRoot) -> None:
        if root.path in seen:
            return
        if not os.path.isabs(root.path):
            return
        entries.append(root)
        seen.add(root.path)

    home = os.environ.get("HOME")
    if home and os.path.isdir(home):
        _add(FsRoot(label="Home", path=home, source="home"))

    for path in normalise_allowlist(list(picker.roots)):
        _add(FsRoot(label=path, path=path, source="allowlist"))

    for root in _connection_roots():
        _add(root)

    _add(FsRoot(label="/", path="/", source="system"))

    return FsRoots(roots=entries, enabled=True)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(
        "This module is imported by the ovid2 web routes; it is not a script."
    )
