"""Golden-file regression harness.

Compares two directory trees produced by the legacy Ovid and Ovid2
implementations for the same input batch. Two levels of granularity:

- ``compare_directories`` — set-diff of CSVs, plus per-file row/column diffs.
- ``compare_csv`` — row-multiset diff (row order ignored) with optional
  masked columns, composite-key pairing, and per-column mismatch samples.

The tool intentionally works on the emitted CSVs (which is what Snowflake
ingests) rather than on Spark DataFrames, so the same code path can compare
Docker-built output against Ovid2's output without a Spark session.

Parity semantics (see ``docs/parity.md``):

* Headers must be equal (same names, same order) unless
  ``ignore_column_order`` is set.
* Rows are compared as a multiset — ordering is irrelevant because neither
  engine guarantees part ordering.
* ``mask_columns`` (default on the CLI: ``UPDATED``) are blanked before
  hashing — wall-clock timestamps can never agree between two runs.
* When ``key_columns`` are supplied, rows left over after the multiset
  diff are paired on the key so the report can say *which columns*
  disagree. A file whose key multisets agree but whose non-key values do
  not is reported as ``key_match`` — the signature of an arbitrary
  ``dropDuplicates`` winner, which is nondeterministic even between two
  legacy runs.

Legacy and Ovid2 both write tab-delimited CSVs with quoting disabled, so
the reader uses :data:`csv.QUOTE_NONE` — a ``"`` inside a value is data.
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import hashlib
import json
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: Statuses that count as parity.
MATCH_STATUSES: frozenset[str] = frozenset({"match"})

#: Upper bound on leftover rows held in memory for key pairing.
_MAX_LEFTOVER_ROWS: int = 200_000


@dataclass
class FileDiff:
    """Per-file comparison outcome.

    Attributes:
        path: File name (left-hand name when prefixes differ).
        status: ``match`` | ``only_a`` | ``only_b`` | ``column_diff`` |
            ``row_diff`` | ``key_match``.
        detail: Human-readable one-liner.
        rows_a: Data rows in the left file.
        rows_b: Data rows in the right file.
        only_a: Rows (multiset) present only on the left.
        only_b: Rows (multiset) present only on the right.
        column_mismatches: For key-paired leftover rows, column → count of
            pairs whose values differ.
        samples: Up to ``sample_limit`` example disagreements.
    """

    path: str
    status: str
    detail: str = ""
    rows_a: int = 0
    rows_b: int = 0
    only_a: int = 0
    only_b: int = 0
    column_mismatches: dict[str, int] = field(default_factory=dict)
    samples: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DirectoryDiff:
    """Overall comparison outcome for two directories."""

    a: Path
    b: Path
    files: list[FileDiff] = field(default_factory=list)

    @property
    def match(self) -> bool:
        """True when every file compares equal."""
        return all(f.status in MATCH_STATUSES for f in self.files)

    def summary(self) -> str:
        """Return a printable summary."""
        matches = sum(1 for f in self.files if f.status in MATCH_STATUSES)
        return f"{matches}/{len(self.files)} files matched"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "a": str(self.a),
            "b": str(self.b),
            "match": self.match,
            "summary": self.summary(),
            "files": [asdict(f) for f in self.files],
        }


# ---------------------------------------------------------------------------
# CSV utilities
# ---------------------------------------------------------------------------


def _reader(fh: Any, delimiter: str) -> Iterator[list[str]]:
    """Return a QUOTE_NONE CSV reader (legacy + Ovid2 write unquoted)."""
    csv.field_size_limit(sys.maxsize)
    return csv.reader(fh, delimiter=delimiter, quoting=csv.QUOTE_NONE)


def _iter_rows(path: Path, delimiter: str) -> Iterator[tuple[str, ...]]:
    """Yield tuples for every data row in ``path`` (header excluded)."""
    with path.open("r", encoding="utf-8", newline="", errors="surrogateescape") as fh:
        reader = _reader(fh, delimiter)
        try:
            next(reader)  # discard header — compared separately
        except StopIteration:
            return
        for row in reader:
            yield tuple(cell.rstrip() for cell in row)


def _read_header(path: Path, delimiter: str) -> list[str]:
    """Return the header row of ``path`` (empty list when the file is empty)."""
    with path.open("r", encoding="utf-8", newline="", errors="surrogateescape") as fh:
        return [c.strip() for c in next(_reader(fh, delimiter), [])]


def _row_hash(row: Sequence[str]) -> str:
    """Return a stable hash for ``row``."""
    return hashlib.sha256("\x1f".join(row).encode("utf-8", errors="surrogateescape")).hexdigest()


def _project(row: tuple[str, ...], order: Sequence[int], masked: frozenset[int]) -> tuple[str, ...]:
    """Reorder ``row`` by ``order`` and blank masked positions."""
    out: list[str] = []
    for i in order:
        value = row[i] if i < len(row) else ""
        out.append("" if i in masked else value)
    return tuple(out)


def _count(
    path: Path, delimiter: str, order: Sequence[int], masked: frozenset[int]
) -> tuple[dict[str, int], int]:
    """Return ``(hash → count, total rows)`` for ``path``."""
    counts: dict[str, int] = {}
    total = 0
    for row in _iter_rows(path, delimiter):
        key = _row_hash(_project(row, order, masked))
        counts[key] = counts.get(key, 0) + 1
        total += 1
    return counts, total


def _collect_leftovers(
    path: Path,
    delimiter: str,
    order: Sequence[int],
    masked: frozenset[int],
    wanted: dict[str, int],
) -> list[tuple[str, ...]]:
    """Second pass: return the rows whose hash is in ``wanted`` (bounded)."""
    remaining = dict(wanted)
    out: list[tuple[str, ...]] = []
    for row in _iter_rows(path, delimiter):
        projected = _project(row, order, masked)
        key = _row_hash(projected)
        n = remaining.get(key, 0)
        if n > 0:
            remaining[key] = n - 1
            out.append(projected)
            if len(out) >= _MAX_LEFTOVER_ROWS:
                break
    return out


# ---------------------------------------------------------------------------
# Comparators
# ---------------------------------------------------------------------------


def _is_masked(column: str, patterns: Iterable[str]) -> bool:
    """Return whether ``column`` matches any mask name or glob pattern."""
    return any(column == p or fnmatch.fnmatchcase(column, p) for p in patterns)


def compare_csv(
    a: Path,
    b: Path,
    *,
    delimiter: str = "|",
    mask_columns: Iterable[str] = (),
    key_columns: Sequence[str] | None = None,
    ignore_column_order: bool = False,
    sample_limit: int = 5,
    name: str | None = None,
) -> FileDiff:
    """Compare two CSVs. Row ordering is ignored (hash-based multiset diff).

    Args:
        a: Path to the "left" (expected / legacy) CSV.
        b: Path to the "right" (Ovid2) CSV.
        delimiter: Field delimiter used by both files.
        mask_columns: Columns blanked on both sides before hashing. Each
            entry is an exact name or an :mod:`fnmatch` pattern
            (``REFRESH_*_DATE``).
        key_columns: Optional composite key used to pair leftover rows
            and attribute disagreements to columns.
        ignore_column_order: Compare by column *name* rather than position.
        sample_limit: Max example disagreements recorded.
        name: Name recorded on the result (defaults to ``a.name``).

    Returns:
        A :class:`FileDiff` summarising the outcome.
    """
    label = name or a.name
    header_a = _read_header(a, delimiter)
    header_b = _read_header(b, delimiter)
    same_set = set(header_a) == set(header_b) and len(header_a) == len(header_b)
    if header_a != header_b and not (ignore_column_order and same_set):
        missing = sorted(set(header_a) - set(header_b))
        added = sorted(set(header_b) - set(header_a))
        detail = ""
        if missing:
            detail += f"missing in b: {missing}; "
        if added:
            detail += f"added in b: {added}"
        if not detail:
            detail = f"column order differs: a={header_a} b={header_b}"
        return FileDiff(path=label, status="column_diff", detail=detail.strip("; "))

    columns = list(header_a)
    order_a = list(range(len(columns)))
    order_b = [header_b.index(c) for c in columns]
    patterns = tuple(mask_columns)
    masked_names = {c for c in columns if _is_masked(c, patterns)}
    masked_a = frozenset(i for i, c in enumerate(columns) if c in masked_names)
    masked_b = frozenset(header_b.index(c) for c in masked_names)

    counts_a, rows_a = _count(a, delimiter, order_a, masked_a)
    counts_b, rows_b = _count(b, delimiter, order_b, masked_b)

    left: dict[str, int] = {}
    right: dict[str, int] = {}
    for key in counts_a.keys() | counts_b.keys():
        diff = counts_a.get(key, 0) - counts_b.get(key, 0)
        if diff > 0:
            left[key] = diff
        elif diff < 0:
            right[key] = -diff
    only_a = sum(left.values())
    only_b = sum(right.values())

    result = FileDiff(
        path=label, status="match", rows_a=rows_a, rows_b=rows_b, only_a=only_a, only_b=only_b
    )
    if only_a == 0 and only_b == 0:
        result.detail = f"{rows_a} rows"
        return result

    result.status = "row_diff"
    result.detail = f"rows a={rows_a} b={rows_b}; only in a: {only_a}, only in b: {only_b}"

    rows_left = _collect_leftovers(a, delimiter, order_a, masked_a, left)
    rows_right = _collect_leftovers(b, delimiter, order_b, masked_b, right)

    key_idx = [columns.index(k) for k in (key_columns or []) if k in columns]
    if not key_idx:
        for row in rows_left[:sample_limit]:
            result.samples.append({"side": "a", "row": dict(zip(columns, row, strict=False))})
        for row in rows_right[:sample_limit]:
            result.samples.append({"side": "b", "row": dict(zip(columns, row, strict=False))})
        return result

    by_key_b: dict[tuple[str, ...], list[tuple[str, ...]]] = {}
    for row in rows_right:
        by_key_b.setdefault(tuple(row[i] for i in key_idx), []).append(row)

    unpaired_a = 0
    for row in rows_left:
        pair_key = tuple(row[i] for i in key_idx)
        bucket = by_key_b.get(pair_key)
        if not bucket:
            unpaired_a += 1
            if len(result.samples) < sample_limit:
                result.samples.append(
                    {
                        "side": "a",
                        "key": list(pair_key),
                        "row": dict(zip(columns, row, strict=False)),
                    }
                )
            continue
        other = bucket.pop()
        diffs = {
            columns[i]: {"a": row[i], "b": other[i]}
            for i in range(len(columns))
            if row[i] != other[i]
        }
        for col in diffs:
            result.column_mismatches[col] = result.column_mismatches.get(col, 0) + 1
        if len(result.samples) < sample_limit:
            result.samples.append({"key": list(pair_key), "diff": diffs})
    unpaired_b = sum(len(v) for v in by_key_b.values())

    if unpaired_a == 0 and unpaired_b == 0 and only_a == only_b:
        result.status = "key_match"
        result.detail += "; every leftover row pairs on the key (non-key values differ)"
    else:
        result.detail += f"; unpaired on key a={unpaired_a} b={unpaired_b}"
    return result


def _strip(name: str, prefix: str) -> str:
    """Return ``name`` with ``prefix`` removed (or ``name`` when absent)."""
    return name[len(prefix) :] if prefix and name.startswith(prefix) else name


def compare_directories(
    a: Path,
    b: Path,
    *,
    delimiter: str = "|",
    a_prefix: str = "",
    b_prefix: str = "",
    tables: Iterable[str] | None = None,
    mask_columns: Iterable[str] = (),
    key_columns: dict[str, Sequence[str]] | None = None,
    ignore_column_order: bool = False,
) -> DirectoryDiff:
    """Compare two directories of pipeline CSV output.

    Files are matched on their *logical* name — the file name with
    ``a_prefix`` / ``b_prefix`` stripped (e.g. legacy ``fixed_death.csv``
    ↔ Ovid2 ``death.csv`` with ``a_prefix='fixed_'``). Only files present
    in *both* directories are diffed row-by-row; files unique to one side
    are recorded as ``only_a`` / ``only_b``.

    Args:
        a: Left (legacy) directory.
        b: Right (Ovid2) directory.
        delimiter: Field delimiter.
        a_prefix: Prefix stripped from left file names.
        b_prefix: Prefix stripped from right file names.
        tables: Optional logical-name filter (``'death'`` → ``death.csv``).
        mask_columns: Columns blanked before hashing.
        key_columns: Per-logical-table composite key for pairing.
        ignore_column_order: Compare by column name, not position.
    """

    def _index(root: Path, prefix: str) -> dict[str, Path]:
        out: dict[str, Path] = {}
        for p in root.glob("*.csv"):
            if prefix and not p.name.startswith(prefix):
                continue
            out[_strip(p.name, prefix)] = p
        return out

    files_a = _index(a, a_prefix)
    files_b = _index(b, b_prefix)
    wanted = {f"{t.lower()}.csv" for t in tables} if tables is not None else None
    all_names = sorted(files_a.keys() | files_b.keys())
    result = DirectoryDiff(a=a, b=b)
    for name in all_names:
        if wanted is not None and name not in wanted:
            continue
        if name in files_a and name not in files_b:
            result.files.append(FileDiff(path=name, status="only_a"))
        elif name in files_b and name not in files_a:
            result.files.append(FileDiff(path=name, status="only_b"))
        else:
            table = name[: -len(".csv")]
            result.files.append(
                compare_csv(
                    files_a[name],
                    files_b[name],
                    delimiter=delimiter,
                    mask_columns=mask_columns,
                    key_columns=(key_columns or {}).get(table),
                    ignore_column_order=ignore_column_order,
                    name=name,
                )
            )
    return result


# ---------------------------------------------------------------------------
# CLI entry point (for standalone invocation)
# ---------------------------------------------------------------------------


def print_diff(diff: DirectoryDiff) -> None:
    """Print a DirectoryDiff in the ``[OK  ] name detail`` format."""
    for entry in diff.files:
        badge = {
            "match": "OK  ",
            "only_a": "MISS",
            "only_b": "EXTRA",
            "key_match": "KEY ",
        }.get(entry.status, "DIFF")
        print(f"[{badge}] {entry.path}  {entry.detail}")
        if entry.column_mismatches:
            worst = sorted(entry.column_mismatches.items(), key=lambda kv: -kv[1])[:10]
            print("         columns: " + ", ".join(f"{c}={n}" for c, n in worst))
    print(diff.summary())


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point (also used by ``ovid2 migrate compare``)."""
    parser = argparse.ArgumentParser(prog="ovid2-golden-diff")
    parser.add_argument("a", help="Legacy Ovid output directory.")
    parser.add_argument("b", help="Ovid2 output directory.")
    parser.add_argument("--delimiter", default="|", help="Field delimiter (default: '|').")
    parser.add_argument("--a-prefix", default="", help="Prefix stripped from left names.")
    parser.add_argument("--b-prefix", default="", help="Prefix stripped from right names.")
    parser.add_argument(
        "--mask",
        nargs="*",
        default=[],
        help="Columns blanked before hashing (names or fnmatch globs, e.g. REFRESH_*_DATE).",
    )
    parser.add_argument("--json", dest="json_out", default=None, help="Write a JSON report.")
    parser.add_argument(
        "--key",
        nargs="*",
        default=[],
        metavar="TABLE=COL[,COL…]",
        help="Composite key per logical table, used to pair leftover rows "
        "(a file whose rows all pair is reported as KEY, not DIFF).",
    )
    args = parser.parse_args(argv)

    diff = compare_directories(
        Path(args.a),
        Path(args.b),
        delimiter=args.delimiter,
        a_prefix=args.a_prefix,
        b_prefix=args.b_prefix,
        mask_columns=args.mask,
        key_columns={
            t.lower(): [c for c in cols.split(",") if c]
            for t, _, cols in (k.partition("=") for k in args.key)
        },
    )
    print_diff(diff)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(diff.to_dict(), indent=2), encoding="utf-8")
    return 0 if diff.match else 1


if __name__ == "__main__":
    raise SystemExit(main())
