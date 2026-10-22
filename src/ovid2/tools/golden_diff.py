"""Golden-file regression harness.

Compares two directory trees produced by the legacy Ovid and Ovid2
implementations for the same input batch. Two levels of granularity:

- ``compare_directories`` — set-diff of CSVs, plus per-file row/column diffs.
- ``compare_csv`` — line-by-line diff with column-aware normalisation
  (trailing whitespace stripped, tolerant of column reordering).

The tool intentionally works on the emitted CSVs (which is what Snowflake
ingests) rather than on Spark DataFrames, so the same code path can compare
Docker-built output against Ovid2's output without a Spark session.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FileDiff:
    """Per-file comparison outcome."""

    path: str
    status: str  # "match" | "only_a" | "only_b" | "column_diff" | "row_diff"
    detail: str = ""


@dataclass
class DirectoryDiff:
    """Overall comparison outcome for two directories."""

    a: Path
    b: Path
    files: list[FileDiff] = field(default_factory=list)

    @property
    def match(self) -> bool:
        """True when every file compares equal."""
        return all(f.status == "match" for f in self.files)

    def summary(self) -> str:
        """Return a printable summary."""
        matches = sum(1 for f in self.files if f.status == "match")
        return f"{matches}/{len(self.files)} files matched"


# ---------------------------------------------------------------------------
# CSV utilities
# ---------------------------------------------------------------------------


def _iter_rows(path: Path, delimiter: str) -> Iterator[tuple[str, ...]]:
    """Yield tuples for every data row in ``path`` (header excluded)."""
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh, delimiter=delimiter)
        try:
            next(reader)  # discard header — compared separately
        except StopIteration:
            return
        for row in reader:
            yield tuple(cell.rstrip() for cell in row)


def _read_header(path: Path, delimiter: str) -> list[str]:
    """Return the header row of ``path`` (empty list when the file is empty)."""
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh, delimiter=delimiter)
        return next(reader, [])


def _row_hash(row: tuple[str, ...]) -> str:
    """Return a stable hash for ``row``."""
    return hashlib.sha256("\x1f".join(row).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Comparators
# ---------------------------------------------------------------------------


def compare_csv(a: Path, b: Path, *, delimiter: str = "|") -> FileDiff:
    """Compare two CSVs. Row ordering is ignored (hash-based multiset diff).

    Args:
        a: Path to the "left" (expected / legacy) CSV.
        b: Path to the "right" (Ovid2) CSV.
        delimiter: Field delimiter used by both files.

    Returns:
        A :class:`FileDiff` summarising the outcome.
    """
    header_a = _read_header(a, delimiter)
    header_b = _read_header(b, delimiter)
    if header_a != header_b:
        missing = sorted(set(header_a) - set(header_b))
        added = sorted(set(header_b) - set(header_a))
        detail = ""
        if missing:
            detail += f"missing in b: {missing}; "
        if added:
            detail += f"added in b: {added}"
        if not detail:
            detail = f"column order differs: a={header_a} b={header_b}"
        return FileDiff(path=a.name, status="column_diff", detail=detail.strip("; "))

    counts_a: dict[str, int] = {}
    counts_b: dict[str, int] = {}
    for row in _iter_rows(a, delimiter):
        key = _row_hash(row)
        counts_a[key] = counts_a.get(key, 0) + 1
    for row in _iter_rows(b, delimiter):
        key = _row_hash(row)
        counts_b[key] = counts_b.get(key, 0) + 1

    only_a = 0
    only_b = 0
    for key in counts_a.keys() | counts_b.keys():
        diff = counts_a.get(key, 0) - counts_b.get(key, 0)
        if diff > 0:
            only_a += diff
        elif diff < 0:
            only_b += -diff

    if only_a == 0 and only_b == 0:
        return FileDiff(path=a.name, status="match", detail=f"{sum(counts_a.values())} rows")
    return FileDiff(
        path=a.name,
        status="row_diff",
        detail=f"only in a: {only_a}, only in b: {only_b}",
    )


def compare_directories(a: Path, b: Path, *, delimiter: str = "|") -> DirectoryDiff:
    """Compare two directories of pipeline CSV output.

    Only files present in *both* directories are diffed row-by-row; files
    unique to one side are recorded as ``only_a`` / ``only_b``.
    """
    files_a = {p.name: p for p in a.glob("*.csv")}
    files_b = {p.name: p for p in b.glob("*.csv")}
    all_names = sorted(files_a.keys() | files_b.keys())
    result = DirectoryDiff(a=a, b=b)
    for name in all_names:
        if name in files_a and name not in files_b:
            result.files.append(FileDiff(path=name, status="only_a"))
        elif name in files_b and name not in files_a:
            result.files.append(FileDiff(path=name, status="only_b"))
        else:
            result.files.append(compare_csv(files_a[name], files_b[name], delimiter=delimiter))
    return result


# ---------------------------------------------------------------------------
# CLI entry point (for standalone invocation)
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point (also used by ``ovid2 migrate compare``)."""
    parser = argparse.ArgumentParser(prog="ovid2-golden-diff")
    parser.add_argument("a", help="Legacy Ovid output directory.")
    parser.add_argument("b", help="Ovid2 output directory.")
    parser.add_argument("--delimiter", default="|", help="Field delimiter (default: '|').")
    args = parser.parse_args(argv)

    diff = compare_directories(Path(args.a), Path(args.b), delimiter=args.delimiter)
    for entry in diff.files:
        badge = {"match": "OK  ", "only_a": "MISS", "only_b": "EXTRA"}.get(entry.status, "DIFF")
        print(f"[{badge}] {entry.path}  {entry.detail}")
    print(diff.summary())
    return 0 if diff.match else 1


if __name__ == "__main__":
    raise SystemExit(main())
