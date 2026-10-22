"""Value object for an Ovid Data Space path.

Grammar (see :doc:the adr-ods-naming-layer design note)::

    /                                  # root, depth 0
    /<pipeline>                         # depth 1
    /<pipeline>/<batch_id>              # depth 2
    /<pipeline>/<batch_id>/<table>      # depth 3, leaf

Components:

- ``pipeline`` — a pipeline package name (``demo``, ``example_pcornet``, ``pcornet_to_omop``, …).
- ``batch_id`` — an opaque batch identifier (typically ``YYYYMMDD``).
- ``table`` — a CDM table name (``DEMOGRAPHIC``, ``ENCOUNTER``, …).

Normalisation:

- Trailing slashes are stripped.
- Empty components are rejected.
- ``..``, ``.``, wildcards, ``@``, ``#``, ``?`` are rejected.
- Case is *preserved* in the stored components, but equality is
  case-insensitive. Rationale: ``DiscAddress._get_read_path`` already
  probes three case variants of the pipeline name, and downstream
  CDM code lowercases table names. Preserving the caller's case
  keeps the URL grep-friendly while making comparison forgiving.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar

__all__ = ["OdsPath", "OdsPathError"]


class OdsPathError(ValueError):
    """Raised when a path string fails the ODS grammar."""


# One consolidated regex is easier to reason about than five small ones.
# Component charset: alphanumerics, underscore, hyphen, dot. Excludes
# every character that has structural meaning elsewhere (``/`` is the
# separator, ``@`` and ``#`` are reserved, ``?`` is query-string in
# HTTP contexts, ``*`` / ``?`` are wildcards, ``..`` walks parents).
_COMPONENT_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")


@dataclass(frozen=True)
class OdsPath:
    """A normalised, immutable ODS path.

    Instances are frozen and hashable — use them as ``dict`` keys or
    ``set`` members. Two paths compare equal case-insensitively via
    :meth:`__eq__` / :meth:`__hash__` (implemented via
    :attr:`_canonical`).
    """

    #: Depth 0..3 following the ``/pipeline/batch/table`` grammar.
    pipeline: str | None = None
    batch_id: str | None = None
    table: str | None = None

    #: Maximum allowed depth. Depth-4+ (column, partition, stage) is
    #: intentionally rejected in v1 — see the ADR alternatives.
    MAX_DEPTH: ClassVar[int] = 3

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def parse(cls, path: str) -> OdsPath:
        """Parse a POSIX-style string into an :class:`OdsPath`.

        Args:
            path: An absolute POSIX-style path. Must start with ``/``.
                Trailing slashes are stripped.

        Returns:
            The normalised path.

        Raises:
            OdsPathError: When the path violates the ODS grammar.
        """
        if not isinstance(path, str):
            raise OdsPathError(f"OdsPath expects a string, got {type(path).__name__}.")

        raw = path.strip()
        if not raw:
            raise OdsPathError("OdsPath cannot be empty.")
        if not raw.startswith("/"):
            raise OdsPathError(f"OdsPath must be absolute (start with '/'); got '{path}'.")

        # Strip trailing slashes, then split. A bare "/" yields [""]
        # so the components list is [] after filtering, which is depth 0.
        stripped = raw.rstrip("/") or "/"

        # Reject double slashes explicitly. ``rstrip('/')`` above already
        # collapsed trailing runs, so any remaining ``//`` sits in the
        # middle of the path — always a grammar error.
        if "//" in stripped:
            raise OdsPathError(f"OdsPath rejects empty components (double slash) in '{path}'.")

        components = [c for c in stripped.split("/") if c != ""]

        if len(components) > cls.MAX_DEPTH:
            raise OdsPathError(
                f"OdsPath depth {len(components)} exceeds max {cls.MAX_DEPTH}: '{path}'. "
                "Column / partition / stage granularity is out of scope in v1."
            )

        for component in components:
            cls._validate_component(component)

        pipeline = components[0] if len(components) >= 1 else None
        batch_id = components[1] if len(components) >= 2 else None
        table = components[2] if len(components) >= 3 else None
        return cls(pipeline=pipeline, batch_id=batch_id, table=table)

    @classmethod
    def _validate_component(cls, component: str) -> None:
        """Raise if ``component`` is not a valid single path segment."""
        if component in (".", ".."):
            raise OdsPathError(f"OdsPath rejects '.' and '..' segments; got '{component}'.")
        if not _COMPONENT_RE.match(component):
            raise OdsPathError(
                f"OdsPath component '{component}' contains invalid characters. "
                "Allowed: alphanumerics, underscore, dot, hyphen; must start alphanumeric."
            )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def depth(self) -> int:
        """Number of set components (0..3)."""
        depth = 0
        if self.pipeline is not None:
            depth += 1
        if self.batch_id is not None:
            depth += 1
        if self.table is not None:
            depth += 1
        return depth

    @property
    def is_root(self) -> bool:
        """``True`` when the path is ``/``."""
        return self.depth == 0

    @property
    def is_leaf(self) -> bool:
        """``True`` when the path names a table (depth 3)."""
        return self.depth == 3

    def as_str(self) -> str:
        """Render the path as an absolute POSIX string."""
        if self.depth == 0:
            return "/"
        parts: list[str] = []
        if self.pipeline is not None:
            parts.append(self.pipeline)
        if self.batch_id is not None:
            parts.append(self.batch_id)
        if self.table is not None:
            parts.append(self.table)
        return "/" + "/".join(parts)

    def parent(self) -> OdsPath:
        """Return the parent path (root is its own parent)."""
        if self.depth == 0:
            return self
        if self.depth == 1:
            return OdsPath()
        if self.depth == 2:
            return OdsPath(pipeline=self.pipeline)
        return OdsPath(pipeline=self.pipeline, batch_id=self.batch_id)

    def child(self, component: str) -> OdsPath:
        """Return a new path with ``component`` appended.

        Args:
            component: One segment. Validated against the grammar.

        Returns:
            The extended path.

        Raises:
            OdsPathError: On depth overflow or invalid component.
        """
        self._validate_component(component)
        if self.depth >= self.MAX_DEPTH:
            raise OdsPathError(
                f"Cannot descend below '{self.as_str()}' — leaf reached at depth {self.MAX_DEPTH}."
            )
        if self.depth == 0:
            return OdsPath(pipeline=component)
        if self.depth == 1:
            return OdsPath(pipeline=self.pipeline, batch_id=component)
        return OdsPath(pipeline=self.pipeline, batch_id=self.batch_id, table=component)

    # ------------------------------------------------------------------
    # Equality / hashing (case-insensitive)
    # ------------------------------------------------------------------

    @property
    def _canonical(self) -> tuple[str, str, str]:
        """Lowercased tuple used for equality + hashing."""
        return (
            (self.pipeline or "").lower(),
            (self.batch_id or "").lower(),
            (self.table or "").lower(),
        )

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
