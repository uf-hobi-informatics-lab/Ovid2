"""Contract: UI rules U1–U3 (``AGENTS.md`` → "UI rules").

U1 — **only relevant fields are shown.** Pipeline create / edit / run
     forms derive visibility from the run-kind registry
     (``ovid2.web.kinds.kind_choices()`` → ``static/field_visibility.js``
     ``data-show-*`` attributes). No JS or template may hard-code kind-name
     sets (``k === 'copy'``, ``new Set(["copy", "move"])``) to toggle
     fields, and no template may toggle visibility with inline
     ``style.display`` scripts.

U2 — **a field that must match a list is a select.** Every catalogue
     field (connections, locations, engines, LLMs) whose value set is
     closed declares ``widget: choices`` / ``bool``; templates never use
     a bare ``<datalist>`` unless new values are explicitly allowed
     (``data-allow-new``). Known closed-set field names are pinned below.

U3 — **explanations live under a "?" icon.** Templates may not render
     catalogue ``help`` / ``note`` text visibly, may not use the
     hint/intro classes (``pl-subtitle``, ``wz-step-sub``, ``nw-hint`` …),
     and may not render long ``note`` / ``muted`` paragraphs. Status,
     error, empty-state and confirmation text is exempt via a small,
     explicit allow-list. JS builds tips with ``Ovid2HelpTip``.

ADR: the adr-ui-hard-rules design note.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

_WEB = Path(__file__).resolve().parents[2] / "src" / "ovid2" / "web"
_TEMPLATES = _WEB / "templates"
_STATIC = _WEB / "static"

#: Templates outside the rules: the manual renders authored prose, and
#: the help / field macros *are* the implementation.
_TEMPLATE_EXEMPT = {"manual.html", "_help.html", "_field.html"}

#: JS outside the rules (vendored / display widgets render runtime data).
_JS_EXEMPT_DIRS = {"vendor", "display"}


def _templates() -> list[Path]:
    return sorted(p for p in _TEMPLATES.glob("*.html") if p.name not in _TEMPLATE_EXEMPT)


def _scripts() -> list[Path]:
    return sorted(
        p
        for p in _STATIC.rglob("*.js")
        if not (set(p.relative_to(_STATIC).parts) & _JS_EXEMPT_DIRS)
    )


def _strip_jinja_comments(text: str) -> str:
    return re.sub(r"\{#.*?#\}", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


# ---------------------------------------------------------------------------
# U1 — relevance is registry-driven
# ---------------------------------------------------------------------------

_KIND_NAMES = "etl|analysis|dq_checks|health|copy|move|export"

#: ``kind === "copy"`` / ``k == 'move'`` / ``new Set(["copy", "move"])``.
_KIND_LITERAL_CMP = re.compile(
    rf"""(?:\b(?:k|kind|kindName|currentKind|s\.kind)\s*[!=]==?\s*["'](?:{_KIND_NAMES})["'])"""
    rf"""|(?:["'](?:{_KIND_NAMES})["']\s*[!=]==?\s*\b(?:k|kind)\b)"""
    rf"""|(?:new\s+Set\(\s*\[\s*["'](?:{_KIND_NAMES})["'])"""
)

#: Files allowed to compare kind literals, with the reason. These compare
#: against a kind for *behaviour* (which API to call / which source kind a
#: report trend accepts), not to decide whether a form field is shown.
_U1_LITERAL_ALLOW: dict[str, str] = {
    "report_library.js": "report-source kinds (health trends), not run-kind field visibility",
    "_run_job_status.html": "export job artefact link, not a form field",
}

#: Inline ``el.style.display = …`` toggles in templates (the pre-U1 idiom).
_STYLE_DISPLAY_TOGGLE = re.compile(r"\.style\.display\s*=")


@pytest.mark.contract
def test_u1_no_hardcoded_kind_names_for_visibility() -> None:
    """Field visibility reads KindSpec flags, never kind-name literals."""
    offenders: list[str] = []
    for path in _scripts() + _templates():
        if path.name in _U1_LITERAL_ALLOW:
            continue
        text = _strip_jinja_comments(path.read_text(encoding="utf-8"))
        for m in _KIND_LITERAL_CMP.finditer(text):
            offenders.append(f"{path.name}:{_line(text, m.start())}: {m.group(0)}")
    assert not offenders, (
        "UI rule U1: decide relevance from the kind's flags "
        "(kind_choices() → data-show-kind-flag / Ovid2Visibility.kindSpec), "
        "not kind-name literals:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.contract
def test_u1_templates_do_not_toggle_style_display() -> None:
    """Templates hide fields via ``data-show-*`` + ``hidden``, not inline scripts."""
    offenders: list[str] = []
    for path in _templates():
        text = path.read_text(encoding="utf-8")
        for script in re.finditer(r"<script\b[^>]*>(.*?)</script>", text, flags=re.S):
            body = script.group(1)
            for m in _STYLE_DISPLAY_TOGGLE.finditer(body):
                offenders.append(f"{path.name}:{_line(text, script.start(1) + m.start())}")
    assert not offenders, (
        "UI rule U1: use data-show-kind / data-show-kind-flag / data-show-when "
        "(static/field_visibility.js) instead of inline style.display toggles:\n  "
        + "\n  ".join(offenders)
    )


#: Pipeline forms that must be wired to the shared visibility module.
_U1_FORMS = ("submit.html", "pipeline_edit.html", "pipeline_new_config.html")


@pytest.mark.parametrize("name", _U1_FORMS)
@pytest.mark.contract
def test_u1_pipeline_forms_use_visibility_module(name: str) -> None:
    """Every pipeline create / edit / run form declares a visibility root."""
    text = (_TEMPLATES / name).read_text(encoding="utf-8")
    assert "data-visibility-root" in text, f"{name}: missing data-visibility-root"
    assert 'id="kind-flags"' in text, f"{name}: missing the kind-flags JSON island"
    assert "data-show-kind-flag" in text, f"{name}: no registry-driven field"


@pytest.mark.contract
def test_u1_kind_choices_carry_flags() -> None:
    """``kind_choices()`` ships every flag the visibility module reads."""
    from ovid2.web.kinds import kind_choices

    needed = {
        "uses_cdm",
        "uses_transformations",
        "tables_mode",
        "allows_extra_mounts",
        "allows_strict_types",
        "health_selection",
        "create_flow",
        "uses_engine_overrides",
    }
    for entry in kind_choices():
        assert needed <= set(entry), f"{entry['name']}: missing {sorted(needed - set(entry))}"


# ---------------------------------------------------------------------------
# U2 — closed value sets are selects
# ---------------------------------------------------------------------------

#: Field names whose value set is closed wherever they appear in a
#: catalogue. Adding a new closed-set field? Add it here *and* give it
#: ``widget: choices``.
_CLOSED_SET_FIELDS = {
    "mount_mode",
    "format",
    "authenticator",
    "signature_version",
    "sslmode",
    "encrypt",
    "trust_server_certificate",
    "serverless",
    "preserve_insertion_order",
    "connection",
}


def _catalogue_fields() -> list[tuple[str, dict[str, Any]]]:
    from ovid2.config import engines, llms
    from ovid2.connections import registry

    out: list[tuple[str, dict[str, Any]]] = []
    for t in registry.list_connection_types():
        spec = registry.connection_field_spec(t)
        for f in spec["args"] + spec["secrets"]:
            out.append((f"connection:{t}", f))
        for f in registry.location_field_spec(t):
            out.append((f"location:{t}", f))
    for t in engines.list_engine_types():
        spec = engines.engine_field_spec(t)
        for f in spec["args"] + spec["secrets"]:
            out.append((f"engine:{t}", f))
    for p in llms.list_providers():
        spec = llms.llm_field_spec(p)
        for f in spec["args"] + spec["secrets"]:
            out.append((f"llm:{p}", f))
    return out


def _widget(f: dict[str, Any]) -> str:
    if f.get("widget") and f.get("widget") != "str":
        return str(f["widget"])
    return str(f.get("type") or "str")


@pytest.mark.contract
def test_u2_closed_set_fields_are_choices() -> None:
    """Known closed-set fields render as a select (or a bool)."""
    offenders = [
        f"{where}.{f['name']} (widget={_widget(f)})"
        for where, f in _catalogue_fields()
        if f["name"] in _CLOSED_SET_FIELDS and _widget(f) not in {"choices", "bool"}
    ]
    assert not offenders, "UI rule U2: closed-set fields must be choices/bool:\n  " + "\n  ".join(
        offenders
    )


@pytest.mark.contract
def test_u2_boolean_like_fields_are_bool() -> None:
    """A field defaulting to "true"/"false" or documented as true/false is a bool."""
    offenders: list[str] = []
    for where, f in _catalogue_fields():
        default = str(f.get("default", "")).lower()
        helptext = str(f.get("help", "")).lower()
        boolish = default in {"true", "false"} or helptext.startswith(("true/false", "'true'"))
        if boolish and _widget(f) not in {"bool", "choices"}:
            offenders.append(f"{where}.{f['name']}")
    assert not offenders, "UI rule U2: boolean fields must use widget=bool:\n  " + "\n  ".join(
        offenders
    )


@pytest.mark.contract
def test_u2_choices_fields_declare_choices() -> None:
    """``widget: choices`` always carries a non-empty static or dynamic list."""
    offenders = [
        f"{where}.{f['name']}"
        for where, f in _catalogue_fields()
        if _widget(f) == "choices" and not (f.get("choices") or f.get("choices_from"))
    ]
    assert not offenders, "choices widget without choices:\n  " + "\n  ".join(offenders)


@pytest.mark.contract
def test_u2_no_bare_datalist() -> None:
    """``<datalist>`` (free text + suggestions) only where new values are allowed."""
    offenders: list[str] = []
    for path in _templates():
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"<input\b[^>]*\blist=\"[^\"]+\"[^>]*>", text, flags=re.S):
            if "data-allow-new" not in m.group(0):
                offenders.append(f"{path.name}:{_line(text, m.start())}")
    assert not offenders, (
        "UI rule U2: use a <select>; keep an input+datalist only when new values are "
        "legitimately allowed, and mark it data-allow-new:\n  " + "\n  ".join(offenders)
    )


#: Template inputs that must be selects (name attribute → reason).
_U2_SELECT_FIELDS = {
    "engine": "an engine config / type",
    "defaults_engine": "an engine config / type",
    "smtp_secret_ref": "a secrets.d entry",
    "transformations": "a pipeline stage",
    "cdm": "a registered CDM",
    "principal": "a role / user",
}


@pytest.mark.contract
def test_u2_known_list_inputs_are_not_text() -> None:
    """Visible text inputs never carry a name that must match a known list."""
    offenders: list[str] = []
    pat = re.compile(r"<input\b(?=[^>]*\btype=\"text\")[^>]*\bname=\"([a-z_]+)\"[^>]*>", re.S)
    for path in _templates():
        text = path.read_text(encoding="utf-8")
        for m in pat.finditer(text):
            tag = m.group(0)
            if m.group(1) in _U2_SELECT_FIELDS and not (
                "readonly" in tag or "data-allow-new" in tag
            ):
                offenders.append(f"{path.name}:{_line(text, m.start())}: {m.group(1)}")
    assert not offenders, "UI rule U2: these must be selects:\n  " + "\n  ".join(offenders)


# ---------------------------------------------------------------------------
# U3 — explanations under a "?" tip
# ---------------------------------------------------------------------------

#: Classes that exist only to show explanatory / intro text.
_HINT_CLASSES = (
    "pl-subtitle",
    "wz-step-sub",
    "nw-hint",
    "nw-legend",
    "rs-canvas-hint",
    "rs-field-hint",
    "wz-kind-tile-desc",
    "nw-template-desc",
    "pl-bound-note",
    "builder-hint",
)

_CLASS_ATTR = re.compile(r"""class=(["'])([^"']*)\1""")
_VISIBLE_HELP = re.compile(r"\{\{\s*(?:f|field|spec|s|row|c|check)\.(?:help|note|hint)\b[^}]*\}\}")
#: Innermost text containers: the body may hold inline markup but no nested
#: block element, so an outer wrapper never hides an inner paragraph.
_PARA = re.compile(
    r"<(p|div|span|small)\b([^>]*)>((?:(?!<(?:p|div|section|table|ul|ol|form)\b).)*?)</\1>", re.S
)
_TAGS = re.compile(r"<[^>]+>|\{\{.*?\}\}|\{%.*?%\}", re.S)

#: Longest visible ``note`` / ``muted`` / ``small`` text allowed (words).
#: Short status lines ("Loading…", "No folders yet.") stay under it.
_MAX_NOTE_WORDS = 8

#: Explicitly exempt visible prose: status, error, empty-state and
#: confirmation text the operator must read without hovering. Keyed by
#: template → substrings that identify the paragraph.
_U3_ALLOW: dict[str, tuple[str, ...]] = {
    "login.html": ('role="alert"', "Sealed local-admin fallback"),
    "pending.html": ("pending approval", "activates your account"),
    "overwrite_confirm.html": ("overwrite", "replace", "keep the existing data"),
    "_terminal_pane.html": ("existing data", "overwrite", "keep the existing data"),
    "install.html": ("On-disk keys", "No <code>ovid2_pipelines", "Finish the pending steps"),
    "install_image_build.html": ("No log output yet",),
    "cdms.html": ("This is unusual",),
}


def _is_allowed(name: str, chunk: str) -> bool:
    return any(s in chunk for s in _U3_ALLOW.get(name, ()))


@pytest.mark.contract
def test_u3_no_hint_classes() -> None:
    """Intro / hint classes are gone — the text lives in a help tip."""
    offenders: list[str] = []
    for path in _templates():
        text = _strip_jinja_comments(path.read_text(encoding="utf-8"))
        for m in _CLASS_ATTR.finditer(text):
            classes = set(m.group(2).split())
            hit = classes & set(_HINT_CLASSES)
            if hit:
                offenders.append(f"{path.name}:{_line(text, m.start())}: {sorted(hit)}")
    for path in _scripts():
        text = path.read_text(encoding="utf-8")
        for cls in _HINT_CLASSES:
            for m in re.finditer(rf"""["' ]{re.escape(cls)}["' ]""", text):
                offenders.append(f"{path.name}:{_line(text, m.start())}: {cls}")
    assert not offenders, (
        "UI rule U3: move explanatory text under help.tip() / Ovid2HelpTip:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.contract
def test_u3_field_help_is_a_tip() -> None:
    """Catalogue ``help`` / ``note`` text is only ever rendered via ``help.tip``."""
    offenders: list[str] = []
    for path in _templates():
        text = _strip_jinja_comments(path.read_text(encoding="utf-8"))
        for m in _VISIBLE_HELP.finditer(text):
            before = text[max(0, m.start() - 40) : m.start()]
            inside_attr = re.search(r"""(title|aria-label|data-[a-z-]+)=["'][^"']*$""", before)
            if "help.tip(" in text[max(0, m.start() - 15) : m.start()] or inside_attr:
                continue
            offenders.append(f"{path.name}:{_line(text, m.start())}: {m.group(0)}")
    # help.tip({{ f.help }}) is written as help.tip(f.help) — a raw
    # ``{{ f.help }}`` in markup is always visible.
    assert not offenders, "UI rule U3: wrap field help in help.tip():\n  " + "\n  ".join(offenders)


@pytest.mark.contract
def test_u3_no_long_visible_notes() -> None:
    """No visible explanatory ``note`` / ``muted`` / ``small`` paragraphs."""
    offenders: list[str] = []
    for path in _templates():
        text = _strip_jinja_comments(path.read_text(encoding="utf-8"))
        for m in _PARA.finditer(text):
            attrs, body = m.group(2), m.group(3)
            if "help-tip" in body or "help.tip" in body:
                body = re.sub(r"\{\{\s*help\.tip.*?\}\}", "", body, flags=re.S)
            cls = _CLASS_ATTR.search(attrs)
            classes = set(cls.group(2).split()) if cls else set()
            # A bare <p> of prose is a visible explanation too.
            bare_p = m.group(1) == "p" and not cls
            if m.group(1) != "small" and not bare_p and not classes & {"note", "muted", "small"}:
                continue
            if classes & {"flash", "flash-error", "flash-ok", "empty", "tree-msg"}:
                continue
            words = _TAGS.sub(" ", body).split()
            if len(words) <= _MAX_NOTE_WORDS or _is_allowed(path.name, m.group(0)):
                continue
            offenders.append(f"{path.name}:{_line(text, m.start())}: {' '.join(words[:8])}…")
    assert not offenders, (
        "UI rule U3: explanatory paragraphs belong under a '?' tip "
        "(help.tip / help.tip_block):\n  " + "\n  ".join(offenders)
    )


@pytest.mark.contract
def test_u3_js_helper_exported() -> None:
    """``help_tip.js`` exposes the client-side tip builder."""
    text = (_STATIC / "help_tip.js").read_text(encoding="utf-8")
    assert "window.Ovid2HelpTip" in text
