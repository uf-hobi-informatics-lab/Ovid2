"""Patient-sample a raw batch folder for legacy ↔ Ovid2 parity runs.

Picks ``N`` patient ids from the batch's patient file and copies every
delimited text file in the folder, keeping only the rows whose patient
column is in the sample (header kept, bytes untouched). Files with no
patient column are copied whole (``provider``, ``care_site``, …), and
files larger than ``--max-unfiltered-bytes`` with no patient column are
truncated to their first ``--head-rows`` rows. Parquet and other binary
files are copied as-is.

The patient column is the first header field (case-insensitive) in
:data:`PATIENT_COLUMNS`; the delimiter is sniffed from the header line.
Quoted fields are honoured, so a quoted delimiter inside a value does not
shift the patient column.

Usage::

    python -m ovid2.tools.parity_sample --src /data/inboxarchive/DEMO/20260916 \\
        --dst <legacy-ovid> --patients 2000 [--seed 7]
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import shutil
import sys
from pathlib import Path

#: Header names treated as the patient identifier, in priority order.
PATIENT_COLUMNS: tuple[str, ...] = (
    "patid",
    "person_id",
    "pat_id",
    "patient_id",
    "traceability_no",
    "mom_patid",
    "patid_1",
)

#: Header names that identify the "patients" file to draw ids from.
PATIENT_FILE_HINTS: tuple[str, ...] = ("demographic", "person", "patient", "fod_deid")

_DELIMS = ("|", "\t", ",", ";", "~")


def _sniff(header: str) -> str:
    counts = {d: header.count(d) for d in _DELIMS}
    best = max(counts, key=counts.__getitem__)
    return best if counts[best] else ","


def _patient_index(fields: list[str]) -> int | None:
    lowered = [f.strip().strip('"').lower() for f in fields]
    for name in PATIENT_COLUMNS:
        if name in lowered:
            return lowered.index(name)
    return None


def _is_text(path: Path) -> bool:
    with path.open("rb") as fh:
        chunk = fh.read(4096)
    return b"\x00" not in chunk and path.suffix.lower() not in (".parquet", ".gz", ".zip", ".xlsx")


def _pick_patients(src: Path, n: int, seed: int) -> set[str]:
    candidates = [p for p in sorted(src.iterdir()) if p.is_file() and _is_text(p)]
    preferred = [p for p in candidates if any(h in p.name.lower() for h in PATIENT_FILE_HINTS)]
    for path in preferred + candidates:
        with path.open("r", encoding="utf-8", errors="surrogateescape", newline="") as fh:
            header = fh.readline()
            delim = _sniff(header)
            idx = _patient_index(next(csv.reader([header], delimiter=delim)))
            if idx is None:
                continue
            ids: list[str] = []
            for row in csv.reader(fh, delimiter=delim):
                if idx < len(row) and row[idx]:
                    ids.append(row[idx])
                if len(ids) >= n * 50:
                    break
        if ids:
            rng = random.Random(seed)  # noqa: S311 - deterministic sampling, not crypto
            uniq = sorted(set(ids))
            rng.shuffle(uniq)
            return set(uniq[:n])
    raise SystemExit(f"no patient file found in {src}")


def sample(
    src: Path,
    dst: Path,
    patients: int,
    *,
    seed: int = 7,
    max_unfiltered_bytes: int = 64 * 1024 * 1024,
    head_rows: int = 20000,
    max_scan_bytes: int | None = None,
    skip_suffixes: tuple[str, ...] = (),
) -> dict[str, int]:
    """Write the sampled folder; return ``{file: rows kept}``.

    ``max_scan_bytes`` bounds how much of each file is filtered (the rest
    is ignored — legacy and Ovid2 both read the same sample, so parity is
    unaffected); ``skip_suffixes`` drops archives / encrypted twins.
    """
    ids = _pick_patients(src, patients, seed)
    dst.mkdir(parents=True, exist_ok=True)
    kept: dict[str, int] = {}
    for path in sorted(src.iterdir()):
        if not path.is_file() or path.name.lower().endswith(skip_suffixes):
            continue
        target = dst / path.name
        if not _is_text(path):
            shutil.copy2(path, target)
            kept[path.name] = -1
            continue
        with path.open("rb") as fin:
            header = fin.readline()
        text_header = header.decode("utf-8", errors="surrogateescape")
        delim = _sniff(text_header)
        idx = _patient_index(next(csv.reader([text_header], delimiter=delim), []))
        count = 0
        with path.open("rb") as fin, target.open("wb") as fout:
            fout.write(fin.readline())
            if idx is None and path.stat().st_size <= max_unfiltered_bytes:
                shutil.copyfileobj(fin, fout)
                kept[path.name] = -1
                continue
            scanned = 0
            for raw in fin:
                scanned += len(raw)
                if max_scan_bytes is not None and scanned > max_scan_bytes:
                    break
                if idx is None:
                    if count >= head_rows:
                        break
                    fout.write(raw)
                    count += 1
                    continue
                line = raw.decode("utf-8", errors="surrogateescape")
                row = next(csv.reader([line], delimiter=delim), [])
                if idx < len(row) and row[idx] in ids:
                    fout.write(raw)
                    count += 1
        kept[path.name] = count
    os.chmod(dst, 0o755)  # noqa: S103 - directory mode per AGENTS.md (0o755 dirs)
    return kept


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(prog="parity-sample")
    parser.add_argument("--src", required=True)
    parser.add_argument("--dst", required=True)
    parser.add_argument("--patients", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--max-scan-gb",
        type=float,
        default=None,
        help="Stop filtering a file after this many GB (multi-TB OMOP feeds).",
    )
    parser.add_argument(
        "--skip-suffix",
        nargs="*",
        default=[".gpg", ".zip", ".gz", ".tar", ".7z", ".pdf"],
        help="File suffixes never copied (archives, encrypted twins).",
    )
    args = parser.parse_args(argv)
    scan = None if args.max_scan_gb is None else int(args.max_scan_gb * 1024**3)
    kept = sample(
        Path(args.src),
        Path(args.dst),
        args.patients,
        seed=args.seed,
        max_scan_bytes=scan,
        skip_suffixes=tuple(args.skip_suffix),
    )
    for name, n in kept.items():
        print(f"{name}: {'copied whole' if n < 0 else f'{n} rows'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
