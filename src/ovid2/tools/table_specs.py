"""Shared parser for ``--tables`` on ``ovid2 tools copy`` / ``move``.

The one-shot copy/move commands accept a list of table specs of the form
``LOGICAL[=SOURCE]``:

- ``LOGICAL`` is the *logical* (CDM/DB) table identifier. It is always
  used as the **destination file stem** (case preserved verbatim). If
  the destination driver appends a format extension (``disc`` /
  ``s3`` / ``azure_blob`` all do), the final artefact is
  ``{LOGICAL}.{fmt}``.

- ``SOURCE`` is the source-side lookup key handed to
  :meth:`Driver.read`. On disc it may be a file name or a glob
  (``*datavant_person_tokens*``); on Snowflake it is a ``dbtable``
  identifier; on S3 / Azure Blob it is a key or key-glob under
  the address prefix (the exact location). When omitted, ``SOURCE`` defaults
  to ``LOGICAL`` — the common case where source basename equals logical
  name.

The rule this codifies: **the output file name mirrors the logical
table identifier, case-preserved**, regardless of the source-side
lookup key. That means an ACME-shaped run of

    ovid2 tools move --from demo_disc --to S3-Testing \\
        --pipeline demo --batch_id 20260610 \\
        --tables 'HASH_TOKEN=*datavant_person_tokens*'

reads ``*datavant_person_tokens*`` from disc but writes
``HASH_TOKEN.csv`` to S3, rather than baking the glob metacharacters
into the destination key.

The parser deliberately keeps the ergonomic single-arg default: every
``--tables T1 T2`` invocation that worked before this feature landed
continues to behave identically (``file_name == table_name``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Characters that mark a value as a glob rather than a bare table
#: name. Present here (not just in the disc resolver) so we can warn
#: operators who accidentally pass a glob as the *logical* name.
_GLOB_METACHARS: tuple[str, ...] = ("*", "?", "[", "]")


class TableSpecError(ValueError):
    """Raised when a ``--tables`` entry is malformed."""


@dataclass(frozen=True)
class TableSpec:
    """A parsed ``--tables`` entry.

    Attributes:
        table_name: Logical / destination identifier. Used verbatim as
            the destination file stem and as the ``table_name=``
            keyword argument on :meth:`Driver.read`.
        file_name: Source-side lookup key. Used as the positional
            ``file_name`` argument on :meth:`Driver.read`. Defaults to
            ``table_name`` when the operator does not supply an
            explicit ``=SOURCE``.
    """

    table_name: str
    file_name: str


def parse_table_spec(spec: str) -> TableSpec:
    """Parse a single ``LOGICAL[=SOURCE]`` string.

    Args:
        spec: The raw string as supplied on the CLI.

    Returns:
        A :class:`TableSpec` with the resolved ``table_name`` and
        ``file_name``.

    Raises:
        TableSpecError: If ``spec`` is empty, contains only
            whitespace, has an empty ``LOGICAL`` or ``SOURCE`` half,
            or contains more than one ``=`` (which we reserve for
            future extensions).
    """
    if spec is None:
        raise TableSpecError("empty table spec")
    stripped = spec.strip()
    if not stripped:
        raise TableSpecError("empty table spec")

    if "=" not in stripped:
        table_name = stripped
        file_name = stripped
    else:
        parts = stripped.split("=")
        if len(parts) != 2:
            raise TableSpecError(
                f"table spec {spec!r} contains more than one '='; the "
                f"grammar is LOGICAL[=SOURCE] with at most one '='."
            )
        left, right = parts[0].strip(), parts[1].strip()
        if not left:
            raise TableSpecError(
                f"table spec {spec!r} has an empty LOGICAL name (left "
                f"of '='); the destination file stem cannot be blank."
            )
        if not right:
            raise TableSpecError(
                f"table spec {spec!r} has an empty SOURCE lookup key "
                f"(right of '='); either drop the '=' to default "
                f"SOURCE to LOGICAL, or fill it in."
            )
        table_name = left
        file_name = right

    if _has_glob_metachar(table_name):
        # Not fatal — an operator may legitimately want a literal ``*``
        # in a destination file stem — but this is almost always a
        # sign they meant to use the split form. Warn once per spec.
        logger.warning(
            "Table spec %r has glob metacharacters in the LOGICAL name; "
            "the destination file will be named %r literally. If you "
            "meant to use %r as the source-side pattern, use the "
            "'LOGICAL=SOURCE' form (e.g. 'HASH_TOKEN=%s').",
            spec,
            table_name,
            table_name,
            table_name,
        )

    return TableSpec(table_name=table_name, file_name=file_name)


def parse_table_specs(specs: Iterable[str]) -> list[TableSpec]:
    """Parse an iterable of ``--tables`` entries.

    Args:
        specs: The raw argv slice ``args.tables``.

    Returns:
        A list of :class:`TableSpec`, one per input entry, in the
        order supplied.

    Raises:
        TableSpecError: If any entry is malformed. The error names
            the offending entry so operators can spot the typo.
    """
    parsed: list[TableSpec] = []
    for spec in specs:
        parsed.append(parse_table_spec(spec))
    return parsed


def _has_glob_metachar(value: str) -> bool:
    """Return ``True`` if ``value`` contains a shell-glob metacharacter."""
    return any(ch in value for ch in _GLOB_METACHARS)


def is_all_sentinel(specs: Iterable[str]) -> bool:
    """Return ``True`` when ``specs`` is exactly the ``all`` sentinel.

    ``copy`` / ``move`` accept ``--tables all`` only for sources whose
    driver can enumerate its own tables (see
    :meth:`ovid2.drivers.base.Driver.expand_all_tables`). The container
    stage detects the sentinel with this helper and, when the source
    supports it, expands it into concrete :class:`TableSpec` entries
    (source key == logical name for each discovered table).

    A single case-insensitive ``all`` token is the sentinel; ``all``
    mixed with other names is *not* (it is treated as a literal table
    spec by :func:`parse_table_specs`, matching pre-sentinel behaviour).
    """
    items = [s.strip() for s in specs if s and s.strip()]
    return len(items) == 1 and items[0].lower() == "all"


__all__ = [
    "TableSpec",
    "TableSpecError",
    "is_all_sentinel",
    "parse_table_spec",
    "parse_table_specs",
]
