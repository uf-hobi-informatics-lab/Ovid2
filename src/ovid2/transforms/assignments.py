"""Per-pipeline transform assignments (re-export).

The implementation lives in :mod:`ovid2.config.transform_assignments` so
the web tier can share it without importing the transforms engine.
"""

from __future__ import annotations

from ovid2.config.transform_assignments import (
    ASSIGNMENTS_FILE,
    Assignment,
    AssignmentsError,
    assignments_path,
    load_assignments,
    parse_assignments,
    table_assignments,
    user_library_dir,
)

__all__ = [
    "ASSIGNMENTS_FILE",
    "Assignment",
    "AssignmentsError",
    "assignments_path",
    "load_assignments",
    "parse_assignments",
    "table_assignments",
    "user_library_dir",
]
