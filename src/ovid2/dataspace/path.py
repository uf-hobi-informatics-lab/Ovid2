"""Value object for an Ovid Data Space path.

Grammar (see :doc:the adr-address-is-exact-location design note)::

    /                                   # root — every storage account
    /<connection>                       # a storage account's top level
    /<connection>/<path>/…/<leaf>       # any folder / schema / table below it

Every data space is the same: a tree of plain directories. The first
segment is the **storage account** (a connection name); the rest is the
location path under it — folders for disc / s3 / azure_blob / gcs,
``DATABASE/SCHEMA[/TABLE]`` for warehouses. There is no ``pipeline`` or
``batch_id`` level: the *complete* path of a folder or schema is what a
run's ``batch_id`` is, and the address of that folder is the exact
location its data is read from / written to.

Normalisation:

- Trailing slashes are stripped.
- Empty components (``//``) are rejected.
- ``..``, ``.``, wildcards, ``@``, ``#``, ``?`` are rejected.
- Case is preserved in the stored components; equality is
  case-insensitive (warehouse identifiers are case-insensitive and most
  operators type folder names inconsistently).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import ClassVar

__all__ = ["OdsPath", "OdsPathError"]


class OdsPathError(ValueError):
    """Raised when a path string fails the ODS grammar."""


# Component charset: alphanumerics, underscore, hyphen, dot, ``$`` (Snowflake
# identifiers). Excludes every character with structural meaning elsewhere
# (``/`` separator, ``@`` / ``#`` reserved, ``?`` query, ``*`` wildcard).
_COMPONENT_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._$\-]*$")


@dataclass(frozen=True)
class OdsPath:
    """A normalised, immutable Data Space path.

    ``connection`` is the storage account; ``parts`` the location path
    beneath it. Instances are frozen and hashable; equality is
    case-insensitive.
    """

    connection: str | None = None
    parts: tuple[str, ...] = field(default_factory=tuple)

    #: Generous ceiling — deep object-store prefixes are legitimate, but an
    #: unbounded path is almost always a bug (or an attack).
    MAX_DEPTH: ClassVar[int] = 32

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def parse(cls, path: str) -> OdsPath:
        """Parse a POSIX-style string into an :class:`OdsPath`.

        Args:
            path: An absolute POSIX-style path (must start with ``/``).

        Raises:
            OdsPathError: When the path violates the grammar.
        """
        if not isinstance(path, str):
            raise OdsPathError(f"OdsPath expects a string, got {type(path).__name__}.")
        raw = path.strip()
        if not raw:
            raise OdsPathError("OdsPath cannot be empty.")
        if not raw.startswith("/"):
            raise OdsPathError(f"OdsPath must be absolute (start with '/'); got '{path}'.")
        stripped = raw.rstrip("/") or "/"
        if "//" in stripped:
            raise OdsPathError(f"OdsPath rejects empty components (double slash) in '{path}'.")
        components = [c for c in stripped.split("/") if c != ""]
        if len(components) > cls.MAX_DEPTH:
            raise OdsPathError(f"OdsPath depth {len(components)} exceeds max {cls.MAX_DEPTH}.")
        for component in components:
            cls._validate_component(component)
        if not components:
            return cls()
        return cls(connection=components[0], parts=tuple(components[1:]))

    @classmethod
    def _validate_component(cls, component: str) -> None:
        """Raise if ``component`` is not a valid single path segment."""
        if component in (".", ".."):
            raise OdsPathError(f"OdsPath rejects '.' and '..' segments; got '{component}'.")
        if not _COMPONENT_RE.match(component):
            raise OdsPathError(
                f"OdsPath component '{component}' contains invalid characters. "
                "Allowed: alphanumerics, underscore, dot, hyphen, '$'."
            )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def depth(self) -> int:
        """Number of components (0 = root)."""
        return 0 if self.connection is None else 1 + len(self.parts)

    @property
    def is_root(self) -> bool:
        """``True`` when the path is ``/``."""
        return self.connection is None

    @property
    def location(self) -> str:
        """The location path under the storage account (``''`` at its top)."""
        return "/".join(self.parts)

    @property
    def name(self) -> str:
        """The last component (``''`` at the root)."""
        if self.parts:
            return self.parts[-1]
        return self.connection or ""

    def as_str(self) -> str:
        """Render the path as an absolute POSIX string."""
        if self.connection is None:
            return "/"
        return "/" + "/".join((self.connection, *self.parts))

    def batch_id(self) -> str:
        """Return the path as a run ``batch_id`` (``<connection>/<location>``)."""
        return self.as_str().lstrip("/")

    def parent(self) -> OdsPath:
        """Return the parent path (root is its own parent)."""
        if self.connection is None:
            return self
        if not self.parts:
            return OdsPath()
        return OdsPath(connection=self.connection, parts=self.parts[:-1])

    def child(self, component: str) -> OdsPath:
        """Return a new path with ``component`` appended.

        Raises:
            OdsPathError: On depth overflow or an invalid component.
        """
        self._validate_component(component)
        if self.depth >= self.MAX_DEPTH:
            raise OdsPathError(f"Cannot descend below '{self.as_str()}' (max depth).")
        if self.connection is None:
            return OdsPath(connection=component)
        return OdsPath(connection=self.connection, parts=(*self.parts, component))

    # ------------------------------------------------------------------
    # Equality / hashing (case-insensitive)
    # ------------------------------------------------------------------

    @property
    def _canonical(self) -> tuple[str, ...]:
        """Lowercased tuple used for equality + hashing."""
        if self.connection is None:
            return ()
        return tuple(c.lower() for c in (self.connection, *self.parts))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, OdsPath):
            return NotImplemented
        return self._canonical == other._canonical

    def __hash__(self) -> int:
        return hash(self._canonical)

    def __str__(self) -> str:  # pragma: no cover — convenience
        return self.as_str()

    def __repr__(self) -> str:  # pragma: no cover — debug only
        return f"OdsPath({self.as_str()!r})"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Library module; import it.")
