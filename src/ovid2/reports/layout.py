"""Report layouts: validation, normalisation and declarative edit ops.

A layout (``report_layout.v1.json``) is the *only* thing a user or the
LLM authors. This module:

- validates it structurally (JSON Schema) and semantically against the
  chart library and, when given, the report source (datasets / columns /
  column types);
- normalises it (fills ids, drops unknown keys the schema forbids);
- applies **edit ops** — the small, closed vocabulary shared by the Studio
  editor, the REST ``POST /api/v1/reports/{id}/ops`` endpoint and the
  assistant's ``edit_report_layout`` UI action. ``OPS`` is pinned against
  the agent-tool catalogue and ``static/report_studio.js`` by
  ``tests/contract/test_report_catalogues.py``.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from jsonschema import Draft202012Validator

from ovid2.reports.catalogue import get_chart, get_theme, list_themes, load_schema

__all__ = [
    "FREE_BLOCK_TYPES",
    "MAX_OPS",
    "OPS",
    "PRIVATE_NOTE_TYPES",
    "STYLE_KEYS",
    "LayoutError",
    "apply_ops",
    "as_template",
    "block_index",
    "empty_layout",
    "iter_blocks",
    "merge_blocks",
    "new_id",
    "normalise",
    "outline",
    "validate_free_block",
    "validate_layout",
]

#: The closed set of edit ops (order = documentation order).
OPS: tuple[str, ...] = (
    "set_report",
    "add_section",
    "update_section",
    "move_section",
    "remove_section",
    "add_block",
    "update_block",
    "move_block",
    "remove_block",
    "set_visibility",
)

#: Upper bound on ops in one batch (UI action / REST call).
MAX_OPS = 40

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")

_REPORT_KEYS = {"title", "subtitle", "description", "theme", "page", "options"}
_SECTION_KEYS = {"title", "description", "visible", "page_break_before", "kind", "style"}
_BLOCK_KEYS = {
    "type",
    "title",
    "caption",
    "visible",
    "width",
    "dataset",
    "chart",
    "params",
    "filters",
    "top_n",
    "text",
    "text_ref",
    "tone",
    "style",
    "export",
    "rows",
    "items",
    "size",
    "author",
    "updated_at",
}

#: Free-form block types (no dataset): user content, notes and layout aids.
FREE_BLOCK_TYPES: tuple[str, ...] = (
    "text",
    "callout",
    "note",
    "info",
    "checklist",
    "divider",
    "spacer",
)

#: Fields the server stamps on ``note`` blocks (ignored when sent by a client).
_SERVER_KEYS = ("author", "updated_at")

#: Keys of a component ``style`` object (``report_layout.v1.json#/$defs/style``).
STYLE_KEYS: tuple[str, ...] = (
    "accent",
    "background",
    "text_color",
    "align",
    "emphasis",
    "font_scale",
    "border",
    "padding",
)


class LayoutError(ValueError):
    """Raised when a layout or an edit op is invalid.

    Attributes:
        errors: Every problem found (human-readable, path-prefixed).
    """

    def __init__(self, errors: list[str] | str) -> None:
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("; ".join(self.errors[:8]))


def empty_layout(title: str = "Untitled report") -> dict[str, Any]:
    """Return a minimal valid layout."""
    return {
        "v": 1,
        "title": title,
        "theme": "publication",
        "page": {"size": "letter", "orientation": "portrait"},
        "options": {"show_provenance": True, "show_toc": False},
        "sections": [],
    }


def new_id(seed: str, taken: Iterable[str]) -> str:
    """Return a slug id derived from ``seed`` that is not in ``taken``."""
    used = set(taken)
    base = _SLUG_STRIP.sub("_", (seed or "item").lower()).strip("_")[:32] or "item"
    if not base[0].isalnum():
        base = "s" + base
    cand = base
    n = 2
    while cand in used:
        suffix = f"_{n}"
        cand = base[: 40 - len(suffix)] + suffix
        n += 1
    return cand


def iter_blocks(layout: Mapping[str, Any]) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    """Yield ``(section, block)`` pairs in stacking order."""
    for section in layout.get("sections") or []:
        for block in section.get("blocks") or []:
            yield section, block


def _all_ids(layout: Mapping[str, Any]) -> set[str]:
    ids = {str(s.get("id")) for s in layout.get("sections") or []}
    ids |= {str(b.get("id")) for _s, b in iter_blocks(layout)}
    return ids


def block_index(layout: Mapping[str, Any]) -> dict[str, tuple[int, int]]:
    """Return ``{block_id: (section_index, block_index)}``."""
    out: dict[str, tuple[int, int]] = {}
    for si, section in enumerate(layout.get("sections") or []):
        for bi, block in enumerate(section.get("blocks") or []):
            out[str(block.get("id"))] = (si, bi)
    return out


def normalise(layout: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deep copy with ids filled, ``v`` set and defaults applied.

    Missing / malformed / duplicate section and block ids are replaced so
    every node is addressable by an edit op.
    """
    out = copy.deepcopy(dict(layout))
    out["v"] = 1
    out.setdefault("title", "Untitled report")
    out.setdefault("sections", [])
    taken: set[str] = set()
    for section in out["sections"]:
        sid = section.get("id")
        if not isinstance(sid, str) or not _ID_RE.match(sid) or sid in taken:
            sid = new_id(str(section.get("title") or "section"), taken)
            section["id"] = sid
        taken.add(sid)
        section.setdefault("blocks", [])
        for block in section["blocks"]:
            bid = block.get("id")
            if not isinstance(bid, str) or not _ID_RE.match(bid) or bid in taken:
                bid = new_id(
                    str(block.get("title") or block.get("chart") or block.get("type") or "block"),
                    taken,
                )
                block["id"] = bid
            taken.add(bid)
    return out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _schema_errors(layout: Any) -> list[str]:
    validator = Draft202012Validator(load_schema("report_layout.v1"))
    errs = sorted(validator.iter_errors(layout), key=lambda e: list(e.absolute_path))
    out: list[str] = []
    for err in errs[:20]:
        path = "/".join(str(p) for p in err.absolute_path) or "(root)"
        out.append(f"{path}: {err.message}")
    return out


def _columns(source: Mapping[str, Any] | None, dataset: str) -> dict[str, str] | None:
    if source is None:
        return None
    ds = (source.get("datasets") or {}).get(dataset)
    if not isinstance(ds, Mapping):
        return None
    return {str(c["name"]): str(c["type"]) for c in ds.get("columns") or []}


def _check_chart_block(
    block: Mapping[str, Any], where: str, source: Mapping[str, Any] | None
) -> list[str]:
    errs: list[str] = []
    kind = get_chart(str(block.get("chart") or ""))
    if kind is None:
        return [f"{where}: unknown chart kind {block.get('chart')!r}"]
    dataset = block.get("dataset")
    if not isinstance(dataset, str):
        return [f"{where}: a chart needs a dataset"]
    params = block.get("params") or {}
    validator = Draft202012Validator(kind.params)
    for err in sorted(validator.iter_errors(params), key=lambda e: list(e.absolute_path))[:6]:
        path = "/".join(str(p) for p in err.absolute_path)
        errs.append(f"{where}.params{('/' + path) if path else ''}: {err.message}")
    cols = _columns(source, dataset)
    if source is not None and cols is None:
        errs.append(f"{where}: dataset {dataset!r} is not in this report's source")
        return errs
    if cols is None:
        return errs
    multi = kind.multi_field_params()
    for pname, allowed in kind.field_params().items():
        if pname not in params:
            continue
        names = params[pname] if pname in multi else [params[pname]]
        if not isinstance(names, list):
            continue
        for name in names:
            if name not in cols:
                errs.append(f"{where}.params.{pname}: no column {name!r} in dataset {dataset!r}")
            elif allowed and cols[name] not in allowed:
                errs.append(
                    f"{where}.params.{pname}: column {name!r} is {cols[name]}, "
                    f"{kind.name} needs one of {allowed}"
                )
    for f in block.get("filters") or []:
        if f.get("field") not in cols:
            errs.append(f"{where}.filters: no column {f.get('field')!r}")
    top = block.get("top_n")
    if isinstance(top, Mapping) and top.get("by") not in cols:
        errs.append(f"{where}.top_n.by: no column {top.get('by')!r}")
    return errs


def validate_layout(
    layout: Any,
    source: Mapping[str, Any] | None = None,
    *,
    strict_source: bool = True,
) -> list[str]:
    """Return every problem with ``layout`` (empty list = valid).

    Args:
        layout: The candidate layout document.
        source: The report source it will be rendered against. When
            given, datasets / columns / column types are checked too.
        strict_source: When ``False`` source mismatches are not reported
            (used for templates applied to a different source).
    """
    errs = _schema_errors(layout)
    if errs:
        return errs
    theme = layout.get("theme")
    if theme is not None and theme not in {t.name for t in list_themes()}:
        errs.append(f"theme: unknown report theme {theme!r}")
    seen: set[str] = set()
    src = source if strict_source else None
    texts = set((source or {}).get("texts") or {})
    for si, section in enumerate(layout["sections"]):
        if section["id"] in seen:
            errs.append(f"sections/{si}: duplicate id {section['id']!r}")
        seen.add(section["id"])
        for bi, block in enumerate(section["blocks"]):
            where = f"sections/{si}/blocks/{bi} ({block['id']})"
            if block["id"] in seen:
                errs.append(f"{where}: duplicate id")
            seen.add(block["id"])
            btype = block["type"]
            if btype == "chart":
                errs.extend(_check_chart_block(block, where, src))
            elif btype in ("text", "callout"):
                if not block.get("text") and not block.get("text_ref"):
                    errs.append(f"{where}: a {btype} block needs text or text_ref")
                ref = block.get("text_ref")
                if ref and source is not None and strict_source and ref not in texts:
                    errs.append(f"{where}: source has no text {ref!r}")
            errs.extend(_check_free_fields(block, btype, where))
    return errs


def _check_free_fields(block: Mapping[str, Any], btype: str, where: str) -> list[str]:
    """Reject fields that belong to a different block type."""
    errs: list[str] = []
    if btype != "chart":
        stray = sorted(k for k in ("dataset", "chart", "params", "filters", "top_n") if k in block)
        if stray:
            errs.append(f"{where}: a {btype} block cannot have {stray}")
    for key, owner in (("rows", "info"), ("items", "checklist"), ("size", "spacer")):
        if key in block and btype != owner:
            errs.append(f"{where}: only {owner} blocks have {key}")
    return errs


# ---------------------------------------------------------------------------
# Edit ops
# ---------------------------------------------------------------------------


def _find_section(layout: dict[str, Any], sid: Any) -> tuple[int, dict[str, Any]]:
    for i, section in enumerate(layout["sections"]):
        if section.get("id") == sid:
            return i, section
    raise LayoutError(f"no section {sid!r}")


def _find_block(layout: dict[str, Any], bid: Any) -> tuple[dict[str, Any], int, dict[str, Any]]:
    for section in layout["sections"]:
        for i, block in enumerate(section["blocks"]):
            if block.get("id") == bid:
                return section, i, block
    raise LayoutError(f"no block {bid!r}")


def _clamp_index(index: Any, length: int) -> int:
    if not isinstance(index, int) or isinstance(index, bool):
        return length
    idx = int(index)
    if idx < 0:
        idx = length + 1 + idx
    return max(0, min(length, idx))


def _pick(src: Mapping[str, Any], keys: set[str], what: str) -> dict[str, Any]:
    unknown = set(src) - keys
    if unknown:
        raise LayoutError(f"{what}: unknown field(s) {sorted(unknown)}")
    return dict(src)


def _update(node: dict[str, Any], changes: Mapping[str, Any]) -> None:
    """Merge ``changes`` into ``node``; a ``None`` value removes the key."""
    for key, value in changes.items():
        if value is None:
            node.pop(key, None)
        else:
            node[key] = value


def _merge_style(node: dict[str, Any], changes: dict[str, Any], merge: bool) -> None:
    """Fold ``changes['style']`` into the node's style when ``merge`` is set.

    With ``merge_style`` a ``None`` value resets that one key to the theme
    default and an emptied style is removed. Without it ``style`` replaces
    the whole object (``None`` removes it).
    """
    if not merge or not isinstance(changes.get("style"), Mapping):
        return
    merged = dict(node.get("style") or {})
    for key, value in changes.pop("style").items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    if merged:
        node["style"] = merged
    else:
        node.pop("style", None)


def _defaults_for(block: dict[str, Any]) -> None:
    """Give a new free-form block sensible starting content."""
    btype = block.get("type")
    if btype == "info":
        block.setdefault("rows", [{"key": "Item", "value": ""}])
    elif btype == "checklist":
        block.setdefault("items", [{"text": "First item", "done": False}])
    elif btype == "spacer":
        block.setdefault("size", "md")
    elif btype == "note":
        block.setdefault("text", "")
        block.setdefault("tone", "note")


def _stamp(block: dict[str, Any], actor: str | None, now: str | None) -> None:
    """Record who last edited a ``note`` block (server-owned fields)."""
    if block.get("type") != "note":
        return
    if actor:
        block["author"] = actor[:200]
    if now:
        block["updated_at"] = now[:40]


def _apply_one(
    layout: dict[str, Any],
    op: Mapping[str, Any],
    actor: str | None = None,
    now: str | None = None,
) -> str:
    name = op.get("op")
    if name not in OPS:
        raise LayoutError(f"unknown op {name!r}")
    if name == "set_report":
        changes = _pick(op.get("set") or {}, _REPORT_KEYS, "set_report")
        if "theme" in changes and changes["theme"] not in {t.name for t in list_themes()}:
            raise LayoutError(f"unknown report theme {changes['theme']!r}")
        for key in ("page", "options"):
            if key in changes and isinstance(changes[key], Mapping):
                merged = dict(layout.get(key) or {})
                merged.update(changes.pop(key))
                layout[key] = merged
        layout.update(changes)
        theme = get_theme(layout.get("theme")).label if "theme" in changes else None
        return f"report settings updated{f' (theme {theme})' if theme else ''}"
    if name == "add_section":
        title = str(op.get("title") or "New section")
        sid = op.get("section_id") or new_id(title, _all_ids(layout))
        if sid in _all_ids(layout):
            raise LayoutError(f"id {sid!r} already used")
        section: dict[str, Any] = {"id": sid, "title": title, "visible": True, "blocks": []}
        if op.get("description"):
            section["description"] = str(op["description"])
        if op.get("kind") in ("content", "notes"):
            section["kind"] = op["kind"]
        layout["sections"].insert(_clamp_index(op.get("index"), len(layout["sections"])), section)
        return f"added section {title!r}"
    if name == "update_section":
        _i, section = _find_section(layout, op.get("section_id"))
        sec_changes = _pick(op.get("set") or {}, _SECTION_KEYS, "update_section")
        _merge_style(section, sec_changes, bool(op.get("merge_style")))
        _update(section, sec_changes)
        return f"updated section {section['title']!r}"
    if name == "move_section":
        i, section = _find_section(layout, op.get("section_id"))
        layout["sections"].pop(i)
        layout["sections"].insert(_clamp_index(op.get("index"), len(layout["sections"])), section)
        return f"moved section {section['title']!r}"
    if name == "remove_section":
        i, section = _find_section(layout, op.get("section_id"))
        layout["sections"].pop(i)
        return f"removed section {section['title']!r}"
    if name == "add_block":
        _i, section = _find_section(layout, op.get("section_id"))
        raw = op.get("block")
        if not isinstance(raw, Mapping):
            raise LayoutError("add_block needs a block object")
        block = _pick({k: v for k, v in raw.items() if k != "id"}, _BLOCK_KEYS, "add_block")
        block = {k: v for k, v in block.items() if v is not None and k not in _SERVER_KEYS}
        block.setdefault("type", "chart")
        _defaults_for(block)
        _stamp(block, actor, now)
        block.setdefault("visible", True)
        seed = str(block.get("title") or block.get("chart") or block["type"])
        bid = raw.get("id") or new_id(seed, _all_ids(layout))
        if bid in _all_ids(layout):
            raise LayoutError(f"id {bid!r} already used")
        block = {"id": bid, **block}
        section["blocks"].insert(_clamp_index(op.get("index"), len(section["blocks"])), block)
        return f"added {block.get('chart') or block['type']} block to {section['title']!r}"
    if name == "update_block":
        _s, _i, block = _find_block(layout, op.get("block_id"))
        changes = _pick(op.get("set") or {}, _BLOCK_KEYS, "update_block")
        for key in _SERVER_KEYS:
            changes.pop(key, None)
        if op.get("merge_params") and isinstance(changes.get("params"), Mapping):
            merged = dict(block.get("params") or {})
            merged.update(changes.pop("params"))
            block["params"] = {k: v for k, v in merged.items() if v is not None}
        _merge_style(block, changes, bool(op.get("merge_style")))
        _update(block, changes)
        _stamp(block, actor, now)
        return f"updated block {block.get('title') or block['id']!r}"
    if name == "move_block":
        section, i, block = _find_block(layout, op.get("block_id"))
        section["blocks"].pop(i)
        target = section
        if op.get("section_id") not in (None, section["id"]):
            _ti, target = _find_section(layout, op.get("section_id"))
        target["blocks"].insert(_clamp_index(op.get("index"), len(target["blocks"])), block)
        return f"moved block {block.get('title') or block['id']!r}"
    if name == "remove_block":
        section, i, block = _find_block(layout, op.get("block_id"))
        section["blocks"].pop(i)
        return f"removed block {block.get('title') or block['id']!r}"
    # set_visibility
    target_id = op.get("target_id")
    visible = op.get("visible")
    if not isinstance(visible, bool):
        raise LayoutError("set_visibility needs visible: true/false")
    try:
        _i, node = _find_section(layout, target_id)
    except LayoutError:
        _s, _bi, node = _find_block(layout, target_id)
    node["visible"] = visible
    return f"{'showed' if visible else 'hid'} {node.get('title') or target_id!r}"


def apply_ops(
    layout: Mapping[str, Any],
    ops: Sequence[Mapping[str, Any]],
    source: Mapping[str, Any] | None = None,
    *,
    actor: str | None = None,
    now: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Apply ``ops`` atomically and return ``(new_layout, change_notes)``.

    The input is never mutated. After all ops run, the result must pass
    :func:`validate_layout` (against ``source`` when given) or nothing is
    applied.

    Args:
        layout: The layout to edit.
        ops: Edit ops from :data:`OPS`.
        source: Report source to validate datasets / columns against.
        actor: Stamped as ``author`` on note blocks the ops add or change.
        now: ISO timestamp stamped as ``updated_at`` on those notes.

    Raises:
        LayoutError: When an op is malformed, refers to an unknown id, or
            the resulting layout is invalid.
    """
    if not isinstance(ops, (list, tuple)) or not ops:
        raise LayoutError("ops must be a non-empty list")
    if len(ops) > MAX_OPS:
        raise LayoutError(f"at most {MAX_OPS} ops per call")
    work = normalise(layout)
    notes: list[str] = []
    for n, op in enumerate(ops):
        if not isinstance(op, Mapping):
            raise LayoutError(f"op {n}: must be an object")
        try:
            notes.append(_apply_one(work, op, actor, now))
        except LayoutError as exc:
            raise LayoutError([f"op {n} ({op.get('op')}): {e}" for e in exc.errors]) from exc
    errs = validate_layout(work, source)
    if errs:
        raise LayoutError(errs)
    return work, notes


def as_template(layout: Mapping[str, Any]) -> dict[str, Any]:
    """Return a reusable copy of ``layout`` for "Save as template".

    Shared free-form blocks are kept as scaffolding: ``info`` values are
    cleared (labels kept), checklist ticks are reset (items kept) and note
    authorship is dropped, so a "Sign-off" or "Action items" section starts
    empty in every report made from the template.
    """
    out = copy.deepcopy(dict(layout))
    for _s, block in iter_blocks(out):
        for key in _SERVER_KEYS:
            block.pop(key, None)
        if block.get("type") == "info":
            block["rows"] = [
                {"key": r.get("key", ""), "value": ""} for r in block.get("rows") or []
            ]
        elif block.get("type") == "checklist":
            block["items"] = [
                {"text": i.get("text", ""), "done": False} for i in block.get("items") or []
            ]
    return out


#: Block types a private note may be.
PRIVATE_NOTE_TYPES: tuple[str, ...] = ("note", "info", "checklist", "text", "callout")


def validate_free_block(block: Any) -> list[str]:
    """Validate one stand-alone free-form block (a private note).

    The block is checked with the same schema + rules as a layout block by
    wrapping it in a throw-away one-section layout.
    """
    if not isinstance(block, Mapping):
        return ["block must be an object"]
    if block.get("type") not in PRIVATE_NOTE_TYPES:
        return [f"a private note must be one of {list(PRIVATE_NOTE_TYPES)}"]
    probe = {
        "v": 1,
        "title": "probe",
        "sections": [{"id": "probe", "title": "probe", "blocks": [dict(block)]}],
    }
    return [e.replace("sections/0/blocks/0", "block") for e in validate_layout(probe, None)]


def merge_blocks(
    layout: Mapping[str, Any],
    extra: Sequence[tuple[str | None, str | None, Mapping[str, Any]]],
    *,
    orphan_title: str = "My notes",
) -> dict[str, Any]:
    """Return ``layout`` with extra blocks inserted at their anchors.

    Args:
        layout: The base layout (not mutated).
        extra: ``(section_id, after_block_id, block)`` triples. A ``None``
            ``after_block_id`` places the block first in its section; an
            unknown section or block anchor sends it to a trailing
            ``orphan_title`` notes section.
        orphan_title: Title of the section collecting unplaced blocks.
    """
    out = copy.deepcopy(dict(layout))
    out.setdefault("sections", [])
    taken = _all_ids(out)
    orphans: list[dict[str, Any]] = []
    for section_id, after, raw in extra:
        block = dict(raw)
        if block.get("id") in taken:
            block["id"] = new_id(str(block.get("id")), taken)
        taken.add(str(block["id"]))
        target = next((s for s in out["sections"] if s.get("id") == section_id), None)
        if target is None:
            orphans.append(block)
            continue
        ids = [b.get("id") for b in target["blocks"]]
        if after is None:
            target["blocks"].insert(0, block)
        elif after in ids:
            target["blocks"].insert(ids.index(after) + 1, block)
        else:
            orphans.append(block)
    if orphans:
        sid = new_id("my_notes", taken)
        out["sections"].append(
            {"id": sid, "title": orphan_title, "kind": "notes", "visible": True, "blocks": orphans}
        )
    return out


def outline(layout: Mapping[str, Any]) -> dict[str, Any]:
    """Return a compact, id-bearing outline (for the LLM's ui_context)."""
    return {
        "title": layout.get("title"),
        "theme": layout.get("theme"),
        "sections": [
            {
                "id": s.get("id"),
                "title": s.get("title"),
                "visible": s.get("visible", True),
                "kind": s.get("kind", "content"),
                "blocks": [
                    {
                        "id": b.get("id"),
                        "type": b.get("type"),
                        "chart": b.get("chart"),
                        "dataset": b.get("dataset"),
                        "title": b.get("title"),
                        "visible": b.get("visible", True),
                        "export": b.get("export", True),
                    }
                    for b in s.get("blocks") or []
                ],
            }
            for s in layout.get("sections") or []
        ],
    }


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("library module")
