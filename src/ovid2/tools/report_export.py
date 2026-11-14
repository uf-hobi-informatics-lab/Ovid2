"""``ovid2 tools report-export`` — render a Report Studio report to a file.

Executor-tier command behind ``POST /api/v1/reports/{id}/exports``. The
web tier writes a *bundle* (validated layout + report source + metadata)
to a private 0600 file and runs this tool through the scoped broker
(``web/services/_broker.run_tool_json``); the heavy renderers (reportlab,
python-docx, xlsxwriter, vl-convert) therefore never load in the web
process (``tests/contract/test_tier_import_closure.py``). No credentials
are needed — report sources are already-aggregated, durable artifacts.

Prints exactly one JSON object on the last stdout line::

    {"ok": true, "path": ..., "filename": ..., "media_type": ..., "size": n}
    {"ok": false, "message": ...}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

__all__ = ["register"]


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``report-export`` under ``ovid2 tools``."""
    from ovid2.reports.formats import EXPORT_FORMATS

    p = subparsers.add_parser(
        "report-export",
        help="Render a Report Studio bundle (layout + source) to pdf/html/docx/pptx/xlsx/svg/png/md/json.",
    )
    p.add_argument(
        "--bundle", required=True, help="Path to the JSON bundle {layout, source, meta}."
    )
    p.add_argument("--format", dest="fmt", choices=sorted(EXPORT_FORMATS), required=True)
    p.add_argument(
        "--out-dir", dest="out_dir", required=True, help="Existing directory for the output file."
    )
    p.add_argument("--block", default=None, help="svg/png only: export just this chart block id.")
    p.set_defaults(_entry=_entry, _tool="report-export")


def _emit(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, default=str) + "\n")
    sys.stdout.flush()


def _entry(args: argparse.Namespace) -> int:
    from ovid2.reports.layout import validate_layout
    from ovid2.reports.render import ExportError, export_report
    from ovid2.reports.sources import SourceError, validate_source

    try:
        bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
        validate_source(bundle["source"])
        errors = validate_layout(bundle["layout"], bundle["source"])
        if errors:
            raise ExportError("layout invalid: " + "; ".join(errors[:5]))
        out_dir = Path(args.out_dir)
        if not out_dir.is_dir():
            raise ExportError(f"output directory {out_dir} does not exist")
        result = export_report(bundle, args.fmt, out_dir, block_id=args.block)
    except (
        OSError,
        KeyError,
        ValueError,
        ImportError,
        ExportError,
        SourceError,
        RuntimeError,
    ) as exc:
        _emit({"ok": False, "message": f"{type(exc).__name__}: {exc}"})
        return 1
    _emit(
        {
            "ok": True,
            "path": str(result.path),
            "filename": result.filename,
            "media_type": result.media_type,
            "size": result.size,
        }
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run via `ovid2 tools report-export`.")
