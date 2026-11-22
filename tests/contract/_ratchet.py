"""Shrink-only ratchets for transitional contract exemptions.

Some contracts cannot be strict yet (pyspark imports in shared code,
PCORnet literals in fixers, ``OVID2001`` eager-action waivers). Instead of
a free-form allow-list that silently grows, each such debt is a **ratchet**
in ``tests/contract/data/shrink_only.json``:

    {"<ratchet>": {"total": N, "files": {"<path>": n, ...}}}

:func:`check_ratchet` fails when

* the current total exceeds the frozen total, or
* any file's count exceeds its frozen count, or a new file appears; or
* the debt **shrank** but the JSON was not lowered (so the improvement is
  locked in — re-run with ``OVID2_UPDATE_RATCHETS=1`` to rewrite it).

``OVID2_UPDATE_RATCHETS=1`` only ever *lowers* entries; it refuses to raise.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

RATCHET_FILE: Path = Path(__file__).parent / "data" / "shrink_only.json"
UPDATE_ENV: str = "OVID2_UPDATE_RATCHETS"


def _load() -> dict[str, dict[str, object]]:
    if not RATCHET_FILE.is_file():
        return {}
    return json.loads(RATCHET_FILE.read_text(encoding="utf-8"))


def _save(data: dict[str, dict[str, object]]) -> None:
    RATCHET_FILE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def check_ratchet(name: str, current: dict[str, int]) -> None:
    """Assert ``current`` per-file counts never exceed the frozen ratchet ``name``."""
    data = _load()
    frozen = data.get(name)
    total = sum(current.values())
    if frozen is None:
        if os.environ.get(UPDATE_ENV) == "1":
            data[name] = {"total": total, "files": dict(sorted(current.items()))}
            _save(data)
            pytest.skip(f"ratchet {name!r} initialised at {total}")
        pytest.fail(f"ratchet {name!r} missing from {RATCHET_FILE.name}; run with {UPDATE_ENV}=1")
    files = {str(k): int(v) for k, v in dict(frozen.get("files") or {}).items()}  # type: ignore[call-overload]
    # Files absent from this tree (e.g. site pipelines stripped from the
    # public mirror) neither count against nor toward the ratchet.
    repo = RATCHET_FILE.parents[3]
    absent = {path for path in files if not (repo / path).exists()}
    if absent:
        files = {p: c for p, c in files.items() if p not in absent}
        frozen = {**frozen, "total": sum(files.values())}
    grew = [
        f"{path}: {count} > {files.get(path, 0)}"
        for path, count in sorted(current.items())
        if count > files.get(path, 0)
    ]
    assert not grew and total <= int(frozen.get("total", 0)), (  # type: ignore[call-overload]
        f"Ratchet {name!r} may only shrink (frozen total {frozen.get('total')}, now {total}). "
        "Fix the new occurrences instead of adding exemptions:\n" + "\n".join(grew)
    )
    shrank = total < int(frozen.get("total", 0)) or any(  # type: ignore[call-overload]
        current.get(p, 0) < c for p, c in files.items()
    )
    if shrank:
        if os.environ.get(UPDATE_ENV) == "1":
            data[name] = {"total": total, "files": dict(sorted(current.items()))}
            _save(data)
            return
        pytest.fail(
            f"Ratchet {name!r} shrank to {total} — lock it in: re-run with {UPDATE_ENV}=1 "
            f"(lowers {RATCHET_FILE.name})."
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Imported by the contract tests.")
