"""Report Studio catalogues: chart kinds, report themes, built-in templates.

Everything here is *data* shipped under ``resources/reports/`` and read
with ``yaml`` + ``jsonschema`` only, so the web tier, the MCP-facing REST
API, the LLM executor's prompt builder and the export executor all see
the same catalogue (one source of truth — no web-safe mirror needed).

- ``charts.yaml``             — the curated chart library (kind → params schema);
- ``themes/<name>.yaml``      — report output themes (independent of the page theme);
- ``templates/<kind>/*.yaml`` — recommended layouts per report-source kind.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any

import yaml

__all__ = [
    "DEFAULT_THEME",
    "CatalogueError",
    "ChartKind",
    "ReportTemplate",
    "ReportTheme",
    "chart_categories",
    "get_chart",
    "get_template",
    "get_theme",
    "list_charts",
    "list_templates",
    "list_themes",
    "load_schema",
]

#: Theme used when a layout names none (or an unknown one).
DEFAULT_THEME = "publication"

_RES = "ovid2.resources.reports"


class CatalogueError(ValueError):
    """Raised when a bundled report catalogue file is malformed."""


@dataclass(frozen=True)
class ChartKind:
    """One chart kind from ``charts.yaml``.

    Attributes:
        name: Stable kind slug (``bar``, ``heatmap`` …).
        label: Human label for the gallery.
        category: Gallery category slug.
        renderer: ``vega`` | ``kpi`` | ``table``.
        summary: One-line description (shown to users and the LLM).
        params: JSON Schema for the block's ``params``.
    """

    name: str
    label: str
    category: str
    renderer: str
    summary: str
    params: dict[str, Any]

    @property
    def required(self) -> list[str]:
        """Return the required parameter names."""
        return list(self.params.get("required", []))

    def field_params(self) -> dict[str, list[str]]:
        """Return ``{param: allowed column types}`` for column-valued params."""
        out: dict[str, list[str]] = {}
        for key, prop in self.params.get("properties", {}).items():
            if "x-field" in prop:
                out[key] = list(prop["x-field"])
            elif "x-fields" in prop:
                out[key] = list(prop["x-fields"])
        return out

    def multi_field_params(self) -> set[str]:
        """Return the names of params that hold a *list* of column names."""
        return {k for k, p in self.params.get("properties", {}).items() if "x-fields" in p}

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable catalogue entry."""
        return {
            "name": self.name,
            "label": self.label,
            "category": self.category,
            "renderer": self.renderer,
            "summary": self.summary,
            "params": copy.deepcopy(self.params),
        }


@dataclass(frozen=True)
class ReportTheme:
    """One report theme (``themes/<name>.yaml``)."""

    name: str
    label: str
    description: str
    mode: str
    fonts: dict[str, str]
    colors: dict[str, str]
    palette: list[str]
    sequential: str
    diverging: str
    status: dict[str, str]
    sizes: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable theme."""
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "mode": self.mode,
            "fonts": dict(self.fonts),
            "colors": dict(self.colors),
            "palette": list(self.palette),
            "sequential": self.sequential,
            "diverging": self.diverging,
            "status": dict(self.status),
            "sizes": dict(self.sizes),
        }


@dataclass(frozen=True)
class ReportTemplate:
    """A recommended layout for one source kind."""

    template_id: str
    source_kind: str
    name: str
    description: str
    layout: dict[str, Any] = field(default_factory=dict)
    builtin: bool = True

    def as_dict(self, *, with_layout: bool = False) -> dict[str, Any]:
        """Return the JSON-serialisable template entry."""
        out: dict[str, Any] = {
            "template_id": self.template_id,
            "source_kind": self.source_kind,
            "name": self.name,
            "description": self.description,
            "builtin": self.builtin,
        }
        if with_layout:
            out["layout"] = copy.deepcopy(self.layout)
        return out


def _read_yaml(*parts: str) -> Any:
    ref = resources.files(_RES).joinpath(*parts)
    with ref.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@lru_cache(maxsize=8)
def load_schema(name: str) -> dict[str, Any]:
    """Return a bundled JSON schema (``report_layout.v1`` …) by stem."""
    ref = resources.files("ovid2.resources.schemas") / f"{name}.json"
    with ref.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):  # pragma: no cover - bundled file
        raise CatalogueError(f"schema {name} is not an object")
    return data


@lru_cache(maxsize=1)
def _charts() -> tuple[dict[str, str], dict[str, ChartKind]]:
    raw = _read_yaml("charts.yaml")
    if not isinstance(raw, dict) or raw.get("v") != 1:
        raise CatalogueError("charts.yaml: expected v: 1")
    categories = {str(k): str(v) for k, v in (raw.get("categories") or {}).items()}
    out: dict[str, ChartKind] = {}
    for entry in raw.get("charts") or []:
        name = str(entry["name"])
        if name in out:
            raise CatalogueError(f"charts.yaml: duplicate chart {name!r}")
        cat = str(entry["category"])
        if cat not in categories:
            raise CatalogueError(f"charts.yaml: {name}: unknown category {cat!r}")
        params = entry.get("params") or {}
        if params.get("type") != "object" or params.get("additionalProperties") is not False:
            raise CatalogueError(f"charts.yaml: {name}: params must be a closed object schema")
        out[name] = ChartKind(
            name=name,
            label=str(entry["label"]),
            category=cat,
            renderer=str(entry["renderer"]),
            summary=str(entry["summary"]),
            params=params,
        )
    return categories, out


def chart_categories() -> dict[str, str]:
    """Return ``{category slug: label}`` in gallery order."""
    return dict(_charts()[0])


def list_charts() -> list[ChartKind]:
    """Return every chart kind in catalogue order."""
    return list(_charts()[1].values())


def get_chart(name: str) -> ChartKind | None:
    """Return one chart kind, or ``None``."""
    return _charts()[1].get(name)


@lru_cache(maxsize=1)
def _themes() -> dict[str, ReportTheme]:
    out: dict[str, ReportTheme] = {}
    root = resources.files(_RES) / "themes"
    for ref in sorted(root.iterdir(), key=lambda r: r.name):
        if not ref.name.endswith(".yaml"):
            continue
        raw = _read_yaml("themes", ref.name)
        name = str(raw["name"])
        if f"{name}.yaml" != ref.name:
            raise CatalogueError(f"theme file {ref.name} declares name {name!r}")
        out[name] = ReportTheme(
            name=name,
            label=str(raw["label"]),
            description=str(raw.get("description", "")),
            mode=str(raw.get("mode", "light")),
            fonts={str(k): str(v) for k, v in raw["fonts"].items()},
            colors={str(k): str(v) for k, v in raw["colors"].items()},
            palette=[str(c) for c in raw["palette"]],
            sequential=str(raw["sequential"]),
            diverging=str(raw["diverging"]),
            status={str(k): str(v) for k, v in raw["status"].items()},
            sizes={str(k): int(v) for k, v in raw["sizes"].items()},
        )
    if DEFAULT_THEME not in out:  # pragma: no cover - bundled
        raise CatalogueError(f"default theme {DEFAULT_THEME!r} missing")
    # Publication first, then alphabetical.
    ordered = {DEFAULT_THEME: out[DEFAULT_THEME]}
    ordered.update({k: v for k, v in sorted(out.items()) if k != DEFAULT_THEME})
    return ordered


def list_themes() -> list[ReportTheme]:
    """Return every report theme (default first)."""
    return list(_themes().values())


def get_theme(name: str | None) -> ReportTheme:
    """Return the named theme, falling back to :data:`DEFAULT_THEME`."""
    themes = _themes()
    return themes.get(name or DEFAULT_THEME) or themes[DEFAULT_THEME]


@lru_cache(maxsize=1)
def _templates() -> dict[str, ReportTemplate]:
    out: dict[str, ReportTemplate] = {}
    root = resources.files(_RES) / "templates"
    for kind_dir in sorted(root.iterdir(), key=lambda r: r.name):
        if not kind_dir.is_dir() or kind_dir.name.startswith(("_", ".")):
            continue
        for ref in sorted(kind_dir.iterdir(), key=lambda r: r.name):
            if not ref.name.endswith(".yaml"):
                continue
            raw = _read_yaml("templates", kind_dir.name, ref.name)
            tid = f"{kind_dir.name}.{ref.name[:-5]}"
            out[tid] = ReportTemplate(
                template_id=tid,
                source_kind=kind_dir.name,
                name=str(raw["name"]),
                description=str(raw.get("description", "")),
                layout=dict(raw["layout"]),
            )
    return out


def list_templates(source_kind: str | None = None) -> list[ReportTemplate]:
    """Return the built-in templates, optionally for one source kind."""
    return [t for t in _templates().values() if source_kind in (None, t.source_kind)]


def get_template(template_id: str) -> ReportTemplate | None:
    """Return one built-in template by id (``<kind>.<stem>``)."""
    return _templates().get(template_id)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("library module")
