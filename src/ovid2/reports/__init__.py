"""Report Studio — declarative, LLM-plannable research reports.

Tier-neutral package (stdlib + ``yaml`` + ``jsonschema`` + ``markdown-it``
at import time). It is imported by the web tier, the export executor and
the analysis transformers, so it must never import ``ovid2.core`` /
``ovid2.web`` / engines / SDKs at module level. The heavy renderers
(``ovid2.reports.render.*`` — reportlab, python-docx, xlsxwriter,
vl-convert) are executor-only and imported lazily by
``ovid2 tools report-export``.

Concepts (ADR the adr-report-studio design note):

- **Report source** — a durable bundle of small named datasets produced by
  a run (``report_source.v1.json``), built by a source adapter
  (:mod:`ovid2.reports.sources`).
- **Layout** — ordered sections of ordered blocks
  (``report_layout.v1.json``); charts are a *kind* from the curated
  library plus validated params, never raw Vega-Lite.
- **Render model** — :func:`ovid2.reports.compose.render_model` turns a
  layout + source + report theme into fully resolved blocks (Vega-Lite
  specs, KPI values, table rows, sanitised HTML). The browser preview and
  every exporter consume the same model, so they always agree.
"""

from __future__ import annotations

__all__: list[str] = []
