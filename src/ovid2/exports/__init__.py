"""Table export writers — CSV, Excel, PDF and Word.

Executor-tier only: the ``ovid2 tools export-table`` subprocess streams
rows from a driver's :meth:`~ovid2.drivers.base.Driver.iter_rows` into
one of these writers, and the web tier serves the finished file. The
web tier never imports this package (``tests/contract/
test_web_no_export_imports.py``) — the document libraries are
heavyweight and optional.

Formats:

- ``csv``  — stdlib :mod:`csv`; any single-character delimiter.
- ``xlsx`` — ``xlsxwriter`` in constant-memory mode (one sheet, capped
  at Excel's 1,048,575 data rows).
- ``pdf``  — ``reportlab`` (optional ``export`` extra), landscape
  table with a repeating header.
- ``docx`` — ``python-docx`` (optional ``export`` extra), one table.

Every writer applies the same header-case option (``asis`` / ``upper``
/ ``lower``).
"""

from __future__ import annotations

from ovid2.exports.writers import (
    EXPORT_FORMATS,
    HEADER_CASES,
    ExportError,
    ExportResult,
    apply_header_case,
    export_rows,
    media_type_for,
)

__all__ = [
    "EXPORT_FORMATS",
    "HEADER_CASES",
    "ExportError",
    "ExportResult",
    "apply_header_case",
    "export_rows",
    "media_type_for",
]
