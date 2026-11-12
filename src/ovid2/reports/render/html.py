"""Self-contained HTML export (interactive charts, fully offline).

Inlines the vendored Vega bundle, the shared renderer
(``report_render.js`` / ``report_render.css``) and the render model, so
the file opens anywhere without a network or the Ovid2 server. A static
SVG of each chart is embedded as a ``<noscript>`` fallback.
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any

__all__ = ["write_html"]


def _static(name: str) -> str:
    ref = resources.files("ovid2.web").joinpath("static", *name.split("/"))
    return ref.read_text(encoding="utf-8")


def _script_safe(text: str) -> str:
    """Neutralise ``</script`` inside inline script content."""
    return text.replace("</script", "<\\/script").replace("<!--", "<\\!--")


def write_html(model: Mapping[str, Any], path: Path) -> Path:
    """Write a single-file interactive HTML report."""
    from ovid2.reports.render._common import chart_svg

    fallback: list[str] = []
    for s in model["sections"]:
        for b in s["blocks"]:
            if b["renderer"] == "vega":
                fallback.append(
                    f"<h3>{html.escape(b.get('title') or b['id'])}</h3>{chart_svg(b['spec'])}"
                )
    payload = json.dumps(model, default=str, separators=(",", ":"))
    bg = html.escape(str(model["theme"]["colors"].get("background", "#ffffff")))
    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="Ovid2 Report Studio">
<title>{html.escape(model["title"])}</title>
<style>html,body{{margin:0;background:{bg};}}{_static("report_render.css")}</style>
</head>
<body>
<div id="report"></div>
<noscript>{"".join(fallback)}</noscript>
<script>{_script_safe(_static("vendor/vega.bundle.min.js"))}</script>
<script>{_script_safe(_static("report_render.js"))}</script>
<script type="application/json" id="report-model">{_script_safe(payload)}</script>
<script>
(function () {{
  var model = JSON.parse(document.getElementById("report-model").textContent);
  window.Ovid2ReportRender.render(document.getElementById("report"), model, {{actions: true}});
}})();
</script>
</body>
</html>
"""
    path.write_text(doc, encoding="utf-8")
    return path


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("library module")
