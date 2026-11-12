"""Curated chart library: one Vega-Lite builder per chart kind.

Every chart kind in ``resources/reports/charts.yaml`` with
``renderer: vega`` has a builder here (pinned both ways by
``tests/contract/test_report_catalogues.py``). Each builder receives
already-validated params, the block's (filtered, suppressed) rows, the
dataset's column metadata and a report theme, and returns a complete
Vega-Lite v5 spec with inline data.

The browser preview and the server-side exporters (vl-convert) compile
the **same** spec, so a chart looks identical in the Studio, in an HTML
export, in a PDF and as a standalone SVG/PNG. The page theme of the web
panel is never consulted — only :class:`ReportTheme`.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

from ovid2.reports.catalogue import ReportTheme
from ovid2.reports.data import aggregate

__all__ = ["BUILDERS", "VEGA_LITE_SCHEMA", "build_spec", "theme_config"]

VEGA_LITE_SCHEMA = "https://vega.github.io/schema/vega-lite/v6.json"

Spec = dict[str, Any]
Cols = Mapping[str, Mapping[str, Any]]
Builder = Callable[[Mapping[str, Any], list[dict[str, Any]], Cols, ReportTheme], Spec]

_VL_TYPE = {
    "quantitative": "quantitative",
    "nominal": "nominal",
    "ordinal": "ordinal",
    "temporal": "temporal",
    "boolean": "nominal",
    "text": "nominal",
}


# ---------------------------------------------------------------------------
# Theme → Vega-Lite config
# ---------------------------------------------------------------------------


def theme_config(theme: ReportTheme) -> dict[str, Any]:
    """Return the Vega-Lite ``config`` block for a report theme."""
    c = theme.colors
    s = theme.sizes
    font = theme.fonts.get("body", "sans-serif")
    axis = {
        "labelColor": c["muted"],
        "titleColor": c["text"],
        "domainColor": c["border"],
        "tickColor": c["border"],
        "gridColor": c["grid"],
        "labelFont": font,
        "titleFont": font,
        "labelFontSize": s.get("axis", 10),
        "titleFontSize": s.get("axis", 10) + 1,
        "titleFontWeight": 600,
        "labelLimit": 220,
    }
    return {
        "background": c["background"],
        "font": font,
        "padding": 8,
        "view": {"stroke": None},
        "title": {
            "color": c["text"],
            "font": theme.fonts.get("heading", font),
            "fontSize": s.get("heading", 14),
            "anchor": "start",
        },
        "axis": axis,
        "legend": {
            "labelColor": c["text"],
            "titleColor": c["text"],
            "labelFont": font,
            "titleFont": font,
            "labelFontSize": s.get("axis", 10),
            "titleFontSize": s.get("axis", 10) + 1,
            "orient": "bottom",
            "labelLimit": 200,
        },
        "header": {"labelColor": c["text"], "titleColor": c["text"]},
        "range": {
            "category": list(theme.palette),
            "ordinal": {"scheme": theme.sequential},
            "ramp": {"scheme": theme.sequential},
            "diverging": {"scheme": theme.diverging},
            "heatmap": {"scheme": theme.sequential},
        },
        "mark": {"color": c["accent"], "font": font},
        "bar": {"color": c["accent"]},
        "line": {"color": c["accent"], "strokeWidth": 2},
        "point": {"color": c["accent"], "filled": True},
        "area": {"color": c["accent"]},
        "arc": {"stroke": c["background"]},
        "text": {"color": c["text"], "font": font, "fontSize": s.get("axis", 10)},
        "rule": {"color": c["muted"]},
        "tick": {"color": c["accent"]},
        "rect": {"stroke": c["background"], "strokeWidth": 0.5},
    }


# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------


def _ftype(cols: Cols, name: str) -> str:
    meta = cols.get(name) or {}
    return _VL_TYPE.get(str(meta.get("type", "nominal")), "nominal")


def _title(cols: Cols, name: str, override: Any = None) -> str:
    if isinstance(override, str) and override:
        return override
    meta = cols.get(name) or {}
    return str(meta.get("title") or name)


def _field(cols: Cols, name: str, *, title: Any = None, **extra: Any) -> dict[str, Any]:
    enc: dict[str, Any] = {
        "field": name,
        "type": _ftype(cols, name),
        "title": _title(cols, name, title),
    }
    if enc["type"] == "temporal":
        enc.setdefault("timeUnit", None)
        enc.pop("timeUnit")
    enc.update(extra)
    return enc


def _measure(
    cols: Cols, name: str, agg: str, *, title: Any = None, log: bool = False
) -> dict[str, Any]:
    enc: dict[str, Any] = {
        "field": name,
        "type": "quantitative",
        "title": _title(cols, name, title),
    }
    if agg and agg != "none":
        enc["aggregate"] = agg
        if agg in ("count", "distinct") and not title:
            enc["title"] = f"{agg} of {enc['title']}"
    if log:
        enc["scale"] = {"type": "symlog"}
    return enc


def _color(cols: Cols, name: str | None, scheme: str, theme: ReportTheme) -> dict[str, Any] | None:
    if not name:
        return None
    enc: dict[str, Any] = {"field": name, "type": _ftype(cols, name), "title": _title(cols, name)}
    if enc["type"] == "quantitative":
        enc["scale"] = {"scheme": theme.diverging if scheme == "diverging" else theme.sequential}
        return enc
    if scheme == "status":
        enc["scale"] = {"domain": list(theme.status), "range": list(theme.status.values())}
    elif scheme == "sequential":
        enc["type"] = "ordinal"
        enc["scale"] = {"scheme": theme.sequential}
    elif scheme == "diverging":
        enc["type"] = "ordinal"
        enc["scale"] = {"scheme": theme.diverging}
    else:
        enc["scale"] = {"range": list(theme.palette)}
    return enc


def _tooltip(cols: Cols, p: Mapping[str, Any], base: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tips = [dict(t) for t in base if t]
    if p.get("agg", "none") in ("none", None):
        for extra in p.get("tooltip") or []:
            tips.append({"field": extra, "type": _ftype(cols, extra), "title": _title(cols, extra)})
    return tips


def _sort(p: Mapping[str, Any], channel: str) -> Any:
    order = p.get("sort", "descending")
    if order == "descending":
        return f"-{channel}"
    if order == "ascending":
        return channel
    return None


def _base(rows: list[dict[str, Any]], p: Mapping[str, Any], default_height: int = 260) -> Spec:
    return {
        "$schema": VEGA_LITE_SCHEMA,
        "data": {"values": rows},
        "height": int(p.get("height") or default_height),
    }


def _drop_none(enc: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in enc.items() if v is not None}


def _swap(enc: dict[str, Any], horizontal: bool) -> dict[str, Any]:
    if not horizontal:
        return enc
    swapped: dict[str, Any] = {}
    pairs = {"x": "y", "y": "x", "xOffset": "yOffset", "yOffset": "xOffset", "x2": "y2", "y2": "x2"}
    for k, v in enc.items():
        swapped[pairs.get(k, k)] = v
    return swapped


def _with_labels(spec: Spec, value_channel: str, enabled: bool, fmt: str = ",.4~g") -> Spec:
    if not enabled:
        return spec
    mark = spec.pop("mark")
    enc = spec.pop("encoding")
    text_enc = {k: v for k, v in enc.items() if k not in ("color",)}
    text_enc["text"] = {**enc[value_channel], "format": fmt}
    offset = {"dy": -6} if value_channel == "y" else {"dx": 4, "align": "left"}
    spec["layer"] = [
        {"mark": mark, "encoding": enc},
        {"mark": {"type": "text", **offset, "baseline": "middle"}, "encoding": text_enc},
    ]
    return spec


# ---------------------------------------------------------------------------
# Builders — comparison
# ---------------------------------------------------------------------------


def _bar(p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme) -> Spec:
    horizontal = bool(p.get("horizontal"))
    cat = _field(cols, p["category"], title=p.get("category_title"))
    val = _measure(
        cols,
        p["value"],
        p.get("agg", "sum"),
        title=p.get("value_title"),
        log=bool(p.get("log_scale")),
    )
    sort = _sort(p, "y")
    if sort is not None and cat["type"] != "temporal":
        cat["sort"] = sort
    color = _color(cols, p.get("color"), p.get("color_scheme", "theme"), theme)
    enc: dict[str, Any] = {"x": cat, "y": val, "color": color}
    enc["tooltip"] = _tooltip(cols, p, [cat, val, color or {}])
    enc = _swap(_drop_none(enc), horizontal)
    if horizontal and "sort" in enc["y"]:
        enc["y"]["sort"] = str(enc["y"]["sort"]).replace("y", "x")
    spec = {**_base(rows, p), "mark": {"type": "bar", "tooltip": True}, "encoding": enc}
    return _with_labels(spec, "x" if horizontal else "y", bool(p.get("labels")))


def _grouped_bar(
    p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme
) -> Spec:
    horizontal = bool(p.get("horizontal"))
    cat = _field(cols, p["category"], title=p.get("category_title"))
    val = _measure(cols, p["value"], p.get("agg", "sum"), title=p.get("value_title"))
    grp = _color(cols, p["group"], p.get("color_scheme", "theme"), theme)
    enc: dict[str, Any] = {
        "x": cat,
        "y": val,
        "xOffset": {"field": p["group"], "type": "nominal"},
        "color": grp,
    }
    enc["tooltip"] = _tooltip(cols, p, [cat, grp or {}, val])
    spec = {
        **_base(rows, p),
        "mark": {"type": "bar", "tooltip": True},
        "encoding": _swap(_drop_none(enc), horizontal),
    }
    return _with_labels(spec, "x" if horizontal else "y", bool(p.get("labels")))


def _stacked(
    p: Mapping[str, Any],
    rows: list[dict[str, Any]],
    cols: Cols,
    theme: ReportTheme,
    normalize: bool,
) -> Spec:
    horizontal = bool(p.get("horizontal"))
    cat = _field(cols, p["category"], title=p.get("category_title"))
    val = _measure(cols, p["value"], p.get("agg", "sum"), title=p.get("value_title"))
    if normalize:
        val["stack"] = "normalize"
        val["axis"] = {"format": ".0%"}
        val["title"] = p.get("value_title") or f"share of {val['title']}"
    sort = _sort(p, "y")
    if not normalize and sort is not None and cat["type"] != "temporal":
        cat["sort"] = sort
    color = _color(cols, p["stack_by"], p.get("color_scheme", "theme"), theme)
    enc = _drop_none(
        {"x": cat, "y": val, "color": color, "tooltip": _tooltip(cols, p, [cat, color or {}, val])}
    )
    enc = _swap(enc, horizontal)
    if horizontal and isinstance(enc.get("y"), dict) and "sort" in enc["y"]:
        enc["y"]["sort"] = str(enc["y"]["sort"]).replace("y", "x")
    return {**_base(rows, p), "mark": {"type": "bar", "tooltip": True}, "encoding": enc}


def _stacked_bar(
    p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme
) -> Spec:
    return _stacked(p, rows, cols, theme, normalize=False)


def _normalized_bar(
    p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme
) -> Spec:
    return _stacked(p, rows, cols, theme, normalize=True)


def _lollipop(
    p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme
) -> Spec:
    horizontal = bool(p.get("horizontal", True))
    cat = _field(cols, p["category"], title=p.get("category_title"))
    val = _measure(cols, p["value"], p.get("agg", "sum"), title=p.get("value_title"))
    sort = _sort(p, "x" if horizontal else "y")
    if sort is not None:
        cat["sort"] = sort
    enc = _swap({"x": cat, "y": val}, horizontal)
    zero = "x2" if horizontal else "y2"
    return {
        **_base(rows, p),
        "encoding": enc,
        "layer": [
            {
                "mark": {"type": "rule", "color": theme.colors["muted"], "strokeWidth": 1.5},
                "encoding": {zero: {"datum": 0}},
            },
            {
                "mark": {
                    "type": "point",
                    "filled": True,
                    "size": 90,
                    "tooltip": True,
                    "color": theme.colors["accent"],
                }
            },
        ],
    }


def _dot_plot(
    p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme
) -> Spec:
    cat = _field(cols, p["category"])
    val = _field(cols, p["value"], title=p.get("value_title"))
    if p.get("log_scale"):
        val["scale"] = {"type": "symlog"}
    sort = _sort(p, "x")
    if sort is not None:
        cat["sort"] = sort
    color = _color(cols, p.get("color"), p.get("color_scheme", "theme"), theme)
    enc = _drop_none(
        {
            "y": cat,
            "x": val,
            "color": color,
            "tooltip": _tooltip({**cols}, {**p, "agg": "none"}, [cat, val, color or {}]),
        }
    )
    return {
        **_base(rows, p),
        "mark": {"type": "point", "filled": True, "size": 80, "tooltip": True},
        "encoding": enc,
    }


def _bullet(
    p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme
) -> Spec:
    cat = _field(cols, p["category"])
    val = _field(cols, p["value"], title=p.get("value_title"))
    tgt = _field(cols, p["target"])
    return {
        **_base(rows, p),
        "encoding": {"y": cat},
        "layer": [
            {
                "mark": {"type": "bar", "height": 14, "tooltip": True},
                "encoding": {"x": val, "tooltip": [cat, val, tgt]},
            },
            {
                "mark": {"type": "tick", "thickness": 3, "size": 24, "color": theme.colors["text"]},
                "encoding": {"x": tgt},
            },
        ],
    }


# ---------------------------------------------------------------------------
# Builders — trend
# ---------------------------------------------------------------------------


def _x_axis(cols: Cols, name: str, title: Any) -> dict[str, Any]:
    enc = _field(cols, name, title=title)
    if enc["type"] == "nominal":
        enc["type"] = "ordinal"
    return enc


def _line_like(
    p: Mapping[str, Any],
    rows: list[dict[str, Any]],
    cols: Cols,
    theme: ReportTheme,
    *,
    mark: str,
    interpolate: str | None = None,
    stack: str | None = None,
) -> Spec:
    x = _x_axis(cols, p["x"], p.get("x_title"))
    y = _measure(
        cols, p["y"], p.get("agg", "sum"), title=p.get("y_title"), log=bool(p.get("log_scale"))
    )
    if stack:
        y["stack"] = stack
        if stack == "normalize":
            y["axis"] = {"format": ".0%"}
    color = _color(cols, p.get("series"), p.get("color_scheme", "theme"), theme)
    mark_def: dict[str, Any] = {
        "type": mark,
        "tooltip": True,
        "interpolate": interpolate or p.get("interpolate", "linear"),
    }
    if mark == "line" and p.get("points", True):
        mark_def["point"] = True
    if mark == "area" and not color:
        mark_def["line"] = {"color": theme.colors["accent"]}
        mark_def["opacity"] = 0.35
    enc = _drop_none(
        {"x": x, "y": y, "color": color, "tooltip": _tooltip(cols, p, [x, y, color or {}])}
    )
    return {**_base(rows, p), "mark": mark_def, "encoding": enc}


def _line(p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme) -> Spec:
    return _line_like(p, rows, cols, theme, mark="line")


def _area(p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme) -> Spec:
    return _line_like(p, rows, cols, theme, mark="area")


def _stacked_area(
    p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme
) -> Spec:
    return _line_like(
        p, rows, cols, theme, mark="area", stack="normalize" if p.get("normalize") else "zero"
    )


def _step_line(
    p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme
) -> Spec:
    return _line_like(
        {**p, "points": False}, rows, cols, theme, mark="line", interpolate="step-after"
    )


def _slope(
    p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme
) -> Spec:
    period = _field(cols, p["period"])
    period["type"] = "ordinal"
    val = _field(cols, p["value"], title=p.get("value_title"))
    color = _color(cols, p["series"], p.get("color_scheme", "theme"), theme)
    return {
        **_base(rows, p),
        "encoding": {"x": {**period, "axis": {"labelAngle": 0}}, "y": val, "color": color},
        "layer": [
            {"mark": {"type": "line", "point": {"filled": True, "size": 60}, "tooltip": True}},
        ],
    }


def _sparkline(
    p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme
) -> Spec:
    x = _x_axis(cols, p["x"], None)
    y = _measure(cols, p["y"], p.get("agg", "sum"))
    x["axis"] = None
    y["axis"] = None
    y["scale"] = {"zero": False}
    return {
        **_base(rows, p, 60),
        "mark": {"type": "line", "interpolate": "monotone", "strokeWidth": 1.5, "tooltip": True},
        "encoding": {"x": x, "y": y},
    }


# ---------------------------------------------------------------------------
# Builders — distribution
# ---------------------------------------------------------------------------


def _histogram(
    p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme
) -> Spec:
    x = {
        "field": p["value"],
        "type": "quantitative",
        "bin": {"maxbins": int(p.get("max_bins", 20))},
        "title": _title(cols, p["value"], p.get("value_title")),
    }
    if p.get("weight"):
        y = {
            "field": p["weight"],
            "aggregate": "sum",
            "type": "quantitative",
            "title": _title(cols, p["weight"]),
        }
    else:
        y = {"aggregate": "count", "type": "quantitative", "title": "rows"}
    return {
        **_base(rows, p),
        "mark": {"type": "bar", "tooltip": True, "binSpacing": 1},
        "encoding": {"x": x, "y": y},
    }


def _boxplot(
    p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme
) -> Spec:
    val = _field(cols, p["value"], title=p.get("value_title"))
    if p.get("log_scale"):
        val["scale"] = {"type": "symlog"}
    enc: dict[str, Any] = {"y": val}
    if p.get("category"):
        enc["x"] = _field(cols, p["category"])
        enc["color"] = {
            **_field(cols, p["category"]),
            "legend": None,
            "scale": {"range": list(theme.palette)},
        }
    return {
        **_base(rows, p),
        "mark": {"type": "boxplot", "extent": 1.5},
        "encoding": _swap(enc, bool(p.get("horizontal"))),
    }


def _strip_plot(
    p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme
) -> Spec:
    enc: dict[str, Any] = {"x": _field(cols, p["value"], title=p.get("value_title"))}
    if p.get("category"):
        enc["y"] = _field(cols, p["category"])
    color = _color(cols, p.get("color"), p.get("color_scheme", "theme"), theme)
    if color:
        enc["color"] = color
    enc["tooltip"] = _tooltip(cols, {**p, "agg": "none"}, [enc["x"], enc.get("y") or {}])
    return {
        **_base(rows, p),
        "mark": {"type": "tick", "tooltip": True, "opacity": 0.7},
        "encoding": enc,
    }


def _density(
    p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme
) -> Spec:
    value = p["value"]
    transform: dict[str, Any] = {"density": value, "as": ["value", "density"]}
    bw = float(p.get("bandwidth") or 0)
    if bw > 0:
        transform["bandwidth"] = bw
    enc: dict[str, Any] = {
        "x": {
            "field": "value",
            "type": "quantitative",
            "title": _title(cols, value, p.get("value_title")),
        },
        "y": {"field": "density", "type": "quantitative", "title": "density"},
    }
    if p.get("group"):
        transform["groupby"] = [p["group"]]
        enc["color"] = _color(cols, p["group"], p.get("color_scheme", "theme"), theme)
    return {
        **_base(rows, p),
        "transform": [transform],
        "mark": {"type": "area", "opacity": 0.45, "line": True, "tooltip": True},
        "encoding": enc,
    }


# ---------------------------------------------------------------------------
# Builders — relationship
# ---------------------------------------------------------------------------


def _scatter(
    p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme
) -> Spec:
    x = _field(cols, p["x"], title=p.get("x_title"))
    y = _field(cols, p["y"], title=p.get("y_title"))
    if p.get("log_scale"):
        y["scale"] = {"type": "symlog"}
    color = _color(cols, p.get("color"), p.get("color_scheme", "theme"), theme)
    enc = _drop_none(
        {
            "x": x,
            "y": y,
            "color": color,
            "tooltip": _tooltip(cols, {**p, "agg": "none"}, [x, y, color or {}]),
        }
    )
    point = {
        "mark": {"type": "point", "filled": True, "opacity": 0.75, "tooltip": True},
        "encoding": enc,
    }
    if not p.get("trendline"):
        return {**_base(rows, p), **point}
    trend: dict[str, Any] = {
        "transform": [{"regression": p["y"], "on": p["x"]}],
        "mark": {"type": "line", "color": theme.colors["muted"], "strokeDash": [6, 4]},
        "encoding": {
            "x": {k: v for k, v in x.items() if k != "title"},
            "y": {k: v for k, v in y.items() if k != "title"},
        },
    }
    return {**_base(rows, p), "layer": [point, trend]}


def _bubble(
    p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme
) -> Spec:
    x = _field(cols, p["x"], title=p.get("x_title"))
    y = _field(cols, p["y"], title=p.get("y_title"))
    size = {**_field(cols, p["size"]), "scale": {"range": [20, 1400]}}
    color = _color(cols, p.get("color"), p.get("color_scheme", "theme"), theme)
    enc = _drop_none(
        {
            "x": x,
            "y": y,
            "size": size,
            "color": color,
            "tooltip": _tooltip(cols, {**p, "agg": "none"}, [x, y, size, color or {}]),
        }
    )
    return {
        **_base(rows, p),
        "mark": {
            "type": "circle",
            "opacity": 0.65,
            "stroke": theme.colors["background"],
            "tooltip": True,
        },
        "encoding": enc,
    }


def _heatmap(
    p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme
) -> Spec:
    x = _field(cols, p["x"], title=p.get("x_title"))
    y = _field(cols, p["y"], title=p.get("y_title"))
    for enc_ in (x, y):
        if enc_["type"] == "quantitative":
            enc_["type"] = "ordinal"
    vtype = _ftype(cols, p["value"])
    scheme = p.get("color_scheme", "sequential")
    if vtype == "quantitative":
        color: dict[str, Any] = _measure(cols, p["value"], p.get("agg", "sum"))
        color["scale"] = {"scheme": theme.diverging if scheme == "diverging" else theme.sequential}
    else:
        color = (
            _color(
                cols, p["value"], "status" if scheme in ("status", "sequential") else scheme, theme
            )
            or {}
        )
        if scheme == "sequential" and not any(
            str(r.get(p["value"])) in theme.status for r in rows[:50]
        ):
            color["scale"] = {"range": list(theme.palette)}
    enc = {"x": x, "y": y, "color": color, "tooltip": _tooltip(cols, p, [x, y, color])}
    spec: Spec = {**_base(rows, p), "mark": {"type": "rect", "tooltip": True}, "encoding": enc}
    if p.get("labels"):
        text = dict(color)
        text.pop("scale", None)
        text.pop("legend", None)
        spec = {
            **_base(rows, p),
            "encoding": {"x": x, "y": y},
            "layer": [
                {
                    "mark": {"type": "rect", "tooltip": True},
                    "encoding": {"color": color, "tooltip": enc["tooltip"]},
                },
                {"mark": {"type": "text", "fontSize": 9}, "encoding": {"text": text}},
            ],
        }
    return spec


def _calendar_heatmap(
    p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme
) -> Spec:
    date = p["date"]
    val = _measure(cols, p["value"], p.get("agg", "sum"))
    val["scale"] = {
        "scheme": theme.diverging if p.get("color_scheme") == "diverging" else theme.sequential
    }
    calc = {"calculate": f"toDate(datum[{_js_str(date)}])", "as": "__date"}
    if p.get("granularity", "month") == "day":
        x = {"field": "__date", "timeUnit": "utcweek", "type": "ordinal", "title": "week"}
        y = {"field": "__date", "timeUnit": "utcday", "type": "ordinal", "title": "weekday"}
    else:
        x = {"field": "__date", "timeUnit": "utcmonth", "type": "ordinal", "title": "month"}
        y = {"field": "__date", "timeUnit": "utcyear", "type": "ordinal", "title": "year"}
    return {
        **_base(rows, p),
        "transform": [calc],
        "mark": {"type": "rect", "tooltip": True},
        "encoding": {"x": x, "y": y, "color": val, "tooltip": [y, x, val]},
    }


def _js_str(value: str) -> str:
    """Return a safe single-quoted Vega expression string literal."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


# ---------------------------------------------------------------------------
# Builders — part to whole
# ---------------------------------------------------------------------------


def _arc(
    p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme, inner: float
) -> Spec:
    height = int(p.get("height") or 260)
    radius = max(30, height // 2 - 10)
    theta = {
        "field": p["value"],
        "type": "quantitative",
        "aggregate": "sum",
        "stack": True,
        "title": _title(cols, p["value"]),
    }
    color = _color(cols, p["category"], p.get("color_scheme", "theme"), theme) or {}
    enc = {
        "theta": theta,
        "color": color,
        "order": {"field": p["value"], "aggregate": "sum", "sort": "descending"},
        "tooltip": [color, theta],
    }
    arc: dict[str, Any] = {"type": "arc", "outerRadius": radius, "tooltip": True}
    if inner:
        arc["innerRadius"] = int(radius * inner)
    layers: list[dict[str, Any]] = [{"mark": arc}]
    if p.get("labels"):
        layers.append(
            {
                "mark": {"type": "text", "radius": radius + 14, "fontSize": 10},
                "encoding": {
                    "text": {
                        "field": p["value"],
                        "aggregate": "sum",
                        "type": "quantitative",
                        "format": ",.4~g",
                    }
                },
            }
        )
    return {**_base(rows, p), "encoding": enc, "layer": layers, "view": {"stroke": None}}


def _pie(p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme) -> Spec:
    return _arc(p, rows, cols, theme, 0.0)


def _donut(
    p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme
) -> Spec:
    return _arc(p, rows, cols, theme, 0.55)


def _part_bar(
    p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme
) -> Spec:
    x = {
        "field": p["value"],
        "aggregate": "sum",
        "type": "quantitative",
        "stack": "normalize",
        "axis": {"format": ".0%"},
        "title": None,
    }
    color = _color(cols, p["category"], p.get("color_scheme", "theme"), theme) or {}
    return {
        **_base(rows, p, 70),
        "mark": {"type": "bar", "tooltip": True},
        "encoding": {
            "x": x,
            "color": color,
            "tooltip": [color, {**x, "title": _title(cols, p["value"]), "stack": None}],
        },
    }


# ---------------------------------------------------------------------------
# Builders — single value (Python-aggregated, so no raw rows are embedded)
# ---------------------------------------------------------------------------


def _single(p: Mapping[str, Any], rows: list[dict[str, Any]]) -> tuple[float, float]:
    raw = aggregate(rows, p["value"], p.get("agg", "sum"))
    value = float(raw) if isinstance(raw, (int, float)) else 0.0
    maximum = float(p.get("max") or 100)
    if p.get("max_field"):
        m = aggregate(rows, p["max_field"], "sum")
        if isinstance(m, (int, float)) and m > 0:
            maximum = float(m)
    return value, maximum


def _gauge(
    p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme
) -> Spec:
    value, maximum = _single(p, rows)
    shown = max(0.0, min(value, maximum))
    height = int(p.get("height") or 180)
    radius = max(40, min(height - 20, 160))
    data = [
        {"k": "value", "v": shown, "o": 0},
        {"k": "rest", "v": max(maximum - shown, 0.0), "o": 1},
    ]
    label = p.get("label") or _title(cols, p["value"])
    return {
        "$schema": VEGA_LITE_SCHEMA,
        "height": height,
        "data": {"values": data},
        "layer": [
            {
                "mark": {"type": "arc", "outerRadius": radius, "innerRadius": int(radius * 0.68)},
                "encoding": {
                    "theta": {
                        "field": "v",
                        "type": "quantitative",
                        "stack": True,
                        "scale": {"range": [-math.pi / 2, math.pi / 2]},
                    },
                    "order": {"field": "o"},
                    "color": {
                        "field": "k",
                        "type": "nominal",
                        "legend": None,
                        "scale": {
                            "domain": ["value", "rest"],
                            "range": [theme.colors["accent"], theme.colors["grid"]],
                        },
                    },
                },
            },
            {
                "data": {"values": [{"t": f"{value:,.4g}", "l": label}]},
                "mark": {
                    "type": "text",
                    "fontSize": theme.sizes.get("title", 20),
                    "fontWeight": 700,
                    "dy": -8,
                },
                "encoding": {"text": {"field": "t"}},
            },
            {
                "data": {"values": [{"l": f"{label} (max {maximum:,.4g})"}]},
                "mark": {
                    "type": "text",
                    "fontSize": theme.sizes.get("axis", 10),
                    "dy": 14,
                    "color": theme.colors["muted"],
                },
                "encoding": {"text": {"field": "l"}},
            },
        ],
        "view": {"stroke": None},
    }


def _progress(
    p: Mapping[str, Any], rows: list[dict[str, Any]], cols: Cols, theme: ReportTheme
) -> Spec:
    value, maximum = _single(p, rows)
    shown = max(0.0, min(value, maximum))
    pct = shown / maximum if maximum else 0.0
    label = p.get("label") or _title(cols, p["value"])
    data = [
        {
            "label": label,
            "value": shown,
            "max": maximum,
            "text": f"{value:,.4g} / {maximum:,.4g} ({pct:.0%})",
        }
    ]
    x_scale = {"domain": [0, maximum]}
    return {
        "$schema": VEGA_LITE_SCHEMA,
        "height": int(p.get("height") or 44),
        "data": {"values": data},
        "encoding": {"y": {"field": "label", "type": "nominal", "title": None}},
        "layer": [
            {
                "mark": {"type": "bar", "color": theme.colors["grid"], "cornerRadius": 4},
                "encoding": {
                    "x": {"field": "max", "type": "quantitative", "scale": x_scale, "axis": None}
                },
            },
            {
                "mark": {
                    "type": "bar",
                    "color": theme.colors["accent"],
                    "cornerRadius": 4,
                    "tooltip": True,
                },
                "encoding": {
                    "x": {"field": "value", "type": "quantitative", "scale": x_scale, "axis": None}
                },
            },
            {
                "mark": {"type": "text", "align": "left", "dx": 6, "color": theme.colors["text"]},
                "encoding": {"x": {"datum": 0}, "text": {"field": "text"}},
            },
        ],
    }


BUILDERS: dict[str, Builder] = {
    "bar": _bar,
    "grouped_bar": _grouped_bar,
    "stacked_bar": _stacked_bar,
    "normalized_bar": _normalized_bar,
    "lollipop": _lollipop,
    "dot_plot": _dot_plot,
    "bullet": _bullet,
    "line": _line,
    "area": _area,
    "stacked_area": _stacked_area,
    "step_line": _step_line,
    "slope": _slope,
    "sparkline": _sparkline,
    "histogram": _histogram,
    "boxplot": _boxplot,
    "strip_plot": _strip_plot,
    "density": _density,
    "scatter": _scatter,
    "bubble": _bubble,
    "heatmap": _heatmap,
    "calendar_heatmap": _calendar_heatmap,
    "pie": _pie,
    "donut": _donut,
    "part_bar": _part_bar,
    "gauge": _gauge,
    "progress": _progress,
}


def _with_defaults(kind_params: Mapping[str, Any], params: Mapping[str, Any]) -> dict[str, Any]:
    out = {
        k: v["default"] for k, v in (kind_params.get("properties") or {}).items() if "default" in v
    }
    out.update({k: v for k, v in params.items() if v is not None})
    return out


def build_spec(
    kind: str,
    params: Mapping[str, Any],
    rows: list[dict[str, Any]],
    columns: list[Mapping[str, Any]],
    theme: ReportTheme,
    *,
    title: str | None = None,
    width: int | str = "container",
) -> Spec:
    """Return a themed Vega-Lite spec for one chart block.

    Args:
        kind: Chart kind (must have ``renderer: vega``).
        params: Validated block params (defaults are filled here).
        rows: The block's rows (already filtered / suppressed).
        columns: Dataset column metadata.
        theme: Report theme (never the page theme).
        title: Optional chart title.
        width: ``"container"`` for responsive browser rendering or a
            pixel width for server-side export.
    """
    from ovid2.reports.catalogue import get_chart

    chart = get_chart(kind)
    if chart is None or kind not in BUILDERS:
        raise KeyError(f"no Vega-Lite builder for chart kind {kind!r}")
    cols = {str(c["name"]): c for c in columns}
    spec = BUILDERS[kind](_with_defaults(chart.params, params), rows, cols, theme)
    spec["width"] = width
    spec["autosize"] = (
        {"type": "fit-x", "contains": "padding"} if width == "container" else {"type": "pad"}
    )
    if title:
        spec["title"] = title
    spec["config"] = theme_config(theme)
    return spec


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("library module")
