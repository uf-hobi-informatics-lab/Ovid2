"""Shared helpers for the document renderers (markdown → runs, chart images)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from markdown_it import MarkdownIt

__all__ = ["MdBlock", "Run", "chart_png", "chart_svg", "md_blocks", "status_color"]


@dataclass
class Run:
    """An inline text run."""

    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False


@dataclass
class MdBlock:
    """A block-level markdown element (``p``, ``h1``..``h6``, ``li``, ``code``)."""

    kind: str
    runs: list[Run] = field(default_factory=list)
    level: int = 0


_MD = MarkdownIt("commonmark", {"html": False})


def md_blocks(text: str) -> list[MdBlock]:
    """Parse markdown into a flat list of blocks of styled runs."""
    tokens = _MD.parse(text or "")
    out: list[MdBlock] = []
    kind = "p"
    level = 0
    list_depth = 0
    ordered_counters: list[int] = []
    for tok in tokens:
        if tok.type == "heading_open":
            kind = tok.tag
        elif tok.type in ("bullet_list_open", "ordered_list_open"):
            list_depth += 1
            ordered_counters.append(0 if tok.type == "ordered_list_open" else -1)
        elif tok.type in ("bullet_list_close", "ordered_list_close"):
            list_depth -= 1
            ordered_counters.pop()
        elif tok.type == "list_item_open":
            kind = "li"
            level = list_depth
            if ordered_counters and ordered_counters[-1] >= 0:
                ordered_counters[-1] += 1
        elif tok.type in ("paragraph_close", "heading_close"):
            kind = "li" if list_depth else "p"
        elif tok.type in ("fence", "code_block"):
            out.append(MdBlock("code", [Run(tok.content.rstrip("\n"), code=True)]))
        elif tok.type == "inline":
            runs: list[Run] = []
            bold = italic = False
            for child in tok.children or []:
                if child.type == "strong_open":
                    bold = True
                elif child.type == "strong_close":
                    bold = False
                elif child.type == "em_open":
                    italic = True
                elif child.type == "em_close":
                    italic = False
                elif child.type == "code_inline":
                    runs.append(Run(child.content, bold, italic, code=True))
                elif child.type in ("softbreak", "hardbreak"):
                    runs.append(Run(" " if child.type == "softbreak" else "\n", bold, italic))
                elif child.type == "text":
                    runs.append(Run(child.content, bold, italic))
            prefix = ""
            if kind == "li" and ordered_counters and ordered_counters[-1] > 0:
                prefix = f"{ordered_counters[-1]}. "
            elif kind == "li":
                prefix = "• "
            if prefix:
                runs.insert(0, Run(prefix))
            out.append(MdBlock(kind, runs, level))
    return out


def status_color(theme: Mapping[str, Any], value: Any) -> str | None:
    """Return the theme's semantic colour for a status value, if any."""
    status = theme.get("status") or {}
    return status.get(str(value)) if value is not None else None


def _vl() -> Any:
    try:
        import vl_convert
    except ImportError as exc:  # pragma: no cover - environment guard
        raise RuntimeError(
            "Chart rendering needs the 'reports' extra: pip install 'ovid2[reports]'"
        ) from exc
    return vl_convert


@lru_cache(maxsize=256)
def _svg_cached(spec_json: str) -> str:
    out: str = _vl().vegalite_to_svg(spec_json)
    return out


def chart_svg(spec: Mapping[str, Any]) -> str:
    """Render a Vega-Lite spec to SVG (offline; no network access)."""
    import json

    return _svg_cached(json.dumps(spec, sort_keys=True, default=str))


def chart_png(spec: Mapping[str, Any], scale: float = 2.0) -> bytes:
    """Render a Vega-Lite spec to PNG bytes."""
    data: bytes = _vl().svg_to_png(chart_svg(spec), scale=scale)
    return data


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("library module")
