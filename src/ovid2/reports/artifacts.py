"""Per-run report artifacts written by producers inside the executor.

Producers that are not themselves report generators (the analysis
transformers) append small aggregate records to
``$WORK_DIR/analysis-<instance_id>.jsonl``. ``WORK_DIR`` is bind-mounted at
the identical path inside containers (see ``core/docker.py``), so the
host-side web tier reads the file exactly as it reads the health report
and the event log. The ``analysis`` source adapter folds the records into
a report source.

Records are small by construction (monthly buckets, top-N values) and
appends are serialised with a process-wide lock so parallel table
workers never interleave lines.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

__all__ = ["analysis_artifact_path", "append_analysis_record", "read_analysis_records"]

_LOCK = threading.Lock()


def _work_dir() -> Path:
    from ovid2.config.settings import WORK_DIR

    return Path(os.environ.get("OVID2_WORK_DIR") or WORK_DIR)


def analysis_artifact_path(instance_id: str, work_dir: Path | None = None) -> Path:
    """Return ``<WORK_DIR>/analysis-<instance_id>.jsonl``."""
    return (work_dir or _work_dir()) / f"analysis-{instance_id}.jsonl"


def append_analysis_record(instance_id: str, record: dict[str, Any]) -> None:
    """Append one JSON record (best-effort; never raises into the pipeline)."""
    if not instance_id:
        return
    path = analysis_artifact_path(instance_id)
    line = json.dumps(record, default=str, separators=(",", ":")) + "\n"
    try:
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
    except OSError:
        return


def read_analysis_records(path: Path) -> list[dict[str, Any]]:
    """Return the parsed records of an analysis artifact (bad lines skipped)."""
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        return []
    return out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("library module")
