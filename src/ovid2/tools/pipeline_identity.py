"""Pipeline *identity* constants — catalogue, AST reader and AST rewriter.

A pipeline package's ``pipeline_settings.py`` carries identifiers that
must agree with configuration that lives elsewhere: the HARVEST
network / datamart ids, the Snowflake databases the upload targets, the
Datavant site key, the OMOP ``cdm_source`` metadata, and the CDM
binding itself. When a template is cloned these must be re-personalised,
otherwise the clone silently inherits the template's identity.

This module is the single source of truth for that list
(:data:`IDENTITY_FIELDS`) and provides:

* :func:`read_settings_constants` — the module-level literal constants of
  a settings file, read with :mod:`ast` (never executed), with simple
  ``A = B`` aliases resolved;
* :func:`default_identity` — name-derived defaults for a clone;
* :func:`rewrite_settings` — an AST-guided, formatting-preserving
  rewrite that replaces only the literal value of each targeted
  assignment (following ``A = B`` aliases to the defining literal).

It is pure-Python and import-safe from the web tier (no ``ovid2.core``).
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "IDENTITY_FIELDS",
    "IdentityField",
    "default_identity",
    "identity_fields_for",
    "read_settings_constants",
    "rewrite_settings",
]


@dataclass(frozen=True)
class IdentityField:
    """One identity constant a pipeline package may declare.

    Attributes:
        key: Constant name in ``pipeline_settings.py``.
        label: Human label.
        category: Grouping for the UI (``cdm`` / ``harvest`` /
            ``warehouse`` / ``datavant`` / ``omop_source``).
        must_match: Where else this value must agree.
        derive: How a clone's default is derived — ``keep`` (template
            value), ``db`` (``CHAR_<NAME>``), ``datamart_id``
            (``<NETWORKID><NAME>``), ``name``, ``lower``, ``upper``,
            ``omop_source_name``.
        choices: Allowed values, when the field is an enum.
    """

    key: str
    label: str
    category: str
    must_match: str
    derive: str = "keep"
    choices: tuple[str, ...] = ()


IDENTITY_FIELDS: tuple[IdentityField, ...] = (
    IdentityField(
        "cdm",
        "Target CDM",
        "cdm",
        "A CDM registered in ovid2.cdm.registry; the pipeline config's `cdm` must agree.",
    ),
    IdentityField(
        "NETWORKID",
        "Network id",
        "harvest",
        "HARVEST.NETWORKID; prefix of DATAMARTID ($OVID2_NETWORK_ID when set).",
    ),
    IdentityField("NETWORK_NAME", "Network name", "harvest", "HARVEST.NETWORK_NAME."),
    IdentityField(
        "DATAMARTID",
        "Datamart id",
        "harvest",
        "HARVEST.DATAMARTID — the network-assigned id; composite key with NETWORKID.",
        derive="datamart_id",
    ),
    IdentityField(
        "DATAMART_NAME", "Datamart name", "harvest", "HARVEST.DATAMART_NAME.", derive="name"
    ),
    IdentityField(
        "DATAMART_PLATFORM", "Datamart platform", "harvest", "HARVEST.DATAMART_PLATFORM."
    ),
    IdentityField(
        "DEFAULT_PIPELINE_DB_NAME",
        "Default database",
        "warehouse",
        "A Snowflake database that exists; fallback when the output address names none.",
        derive="db",
    ),
    IdentityField(
        "MERGE_DATABASE_NAME",
        "Merge database",
        "warehouse",
        "Snowflake database the merge upload writes into.",
        derive="db",
    ),
    IdentityField(
        "MERGE_SCHEMA_NAME", "Merge schema", "warehouse", "Schema in the merge database."
    ),
    IdentityField(
        "DEFAULT_UPLOAD_TYPE",
        "Upload type",
        "warehouse",
        "Merge into existing tables or replace them.",
        choices=("merge", "no_merge"),
    ),
    IdentityField(
        "DATAVANT_KEY",
        "Datavant site key",
        "datavant",
        "Datavant `--from` site key; dv_credentials.txt in $OVID2_CREDENTIALS_DIR must cover it.",
        derive="lower",
    ),
    IdentityField(
        "CDM_SOURCE_NAME",
        "CDM source name",
        "omop_source",
        "OMOP cdm_source.cdm_source_name.",
        derive="omop_source_name",
    ),
    IdentityField(
        "CDM_SOURCE_ABBREVIATION",
        "CDM source abbreviation",
        "omop_source",
        "OMOP cdm_source.cdm_source_abbreviation.",
        derive="upper",
    ),
    IdentityField("CDM_HOLDER", "CDM holder", "omop_source", "OMOP cdm_source.cdm_holder."),
)

_BY_KEY = {f.key: f for f in IDENTITY_FIELDS}


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _assignments(tree: ast.Module) -> list[tuple[str, ast.expr, ast.stmt]]:
    """Return ``(name, value, node)`` for every top-level single-name assignment."""
    out: list[tuple[str, ast.expr, ast.stmt]] = []
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            out.append((node.targets[0].id, node.value, node))
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            out.append((node.target.id, node.value, node))
    return out


def read_settings_constants(text: str) -> dict[str, Any]:
    """Return the module-level literal constants of a settings source.

    ``A = <literal>`` yields its value; ``A = B`` yields ``B``'s value when
    ``B`` is itself a constant defined earlier. Non-literal assignments
    are skipped. The file is never executed.

    Args:
        text: ``pipeline_settings.py`` source.

    Returns:
        ``{name: value}`` (last assignment wins, like Python).
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {}
    values: dict[str, Any] = {}
    for name, value, _node in _assignments(tree):
        if isinstance(value, ast.Name):
            if value.id in values:
                values[name] = values[value.id]
            continue
        try:
            values[name] = ast.literal_eval(value)
        except (ValueError, SyntaxError, TypeError):
            continue
    return values


def identity_fields_for(constants: dict[str, Any]) -> list[IdentityField]:
    """Return the identity fields a template declares (``cdm`` always)."""
    return [f for f in IDENTITY_FIELDS if f.key == "cdm" or f.key in constants]


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def _upper(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def default_identity(
    new_name: str,
    template_constants: dict[str, Any],
    *,
    cdm: str,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the identity values a clone named ``new_name`` should carry.

    Only fields the template declares are returned (plus ``cdm``).
    ``overrides`` win; ``DATAMARTID`` is derived from the *effective*
    ``NETWORKID`` so overriding the network id re-derives the datamart id.

    Args:
        new_name: The clone's package name.
        template_constants: :func:`read_settings_constants` of the template.
        cdm: Target CDM name.
        overrides: Operator-supplied values (e.g. from the wizard).
    """
    overrides = {k: v for k, v in (overrides or {}).items() if v not in (None, "")}
    upper = _upper(new_name)
    network = str(
        overrides.get("NETWORKID")
        or os.environ.get("OVID2_NETWORK_ID")
        or template_constants.get("NETWORKID")
        or "NET"
    )
    out: dict[str, Any] = {}
    for field in identity_fields_for(template_constants):
        if field.key == "cdm":
            out["cdm"] = overrides.get("cdm", cdm)
            continue
        if field.key in overrides:
            out[field.key] = overrides[field.key]
            continue
        current = template_constants.get(field.key)
        derive = field.derive
        if field.key == "NETWORKID":
            out[field.key] = network
        elif derive == "db":
            out[field.key] = f"CHAR_{upper}"
        elif derive == "datamart_id":
            out[field.key] = f"{network}{upper}"
        elif derive == "name":
            out[field.key] = new_name
        elif derive == "lower":
            out[field.key] = new_name.lower()
        elif derive == "upper":
            out[field.key] = upper
        elif derive == "omop_source_name":
            out[field.key] = f"{new_name} PCORnet-to-OMOP conversion"
        else:
            out[field.key] = current
    return out


# ---------------------------------------------------------------------------
# Rewriting
# ---------------------------------------------------------------------------


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def _quote_like(original: str, value: Any) -> str:
    """Render ``value`` as a literal, preserving the original quote style."""
    if isinstance(value, str):
        quote = '"' if original.lstrip()[:1] == '"' else "'"
        rendered = repr(value)
        if quote == '"' and rendered.startswith("'") and '"' not in value:
            rendered = '"' + rendered[1:-1].replace("\\'", "'") + '"'
        return rendered
    return repr(value)


def rewrite_settings(text: str, values: dict[str, Any]) -> str:
    """Rewrite literal constants in a settings source, preserving formatting.

    For each ``name`` in ``values``:

    * every top-level ``name = <literal>`` has its literal replaced;
    * ``name = OTHER`` (an alias) rewrites ``OTHER``'s defining literal
      instead — unless ``OTHER`` is itself in ``values``;
    * if the name is not assigned at all and is ``cdm``, a
      ``cdm = "<value>"`` line is appended (other absent names are left
      absent).

    Args:
        text: ``pipeline_settings.py`` source.
        values: ``{constant: new value}``.

    Returns:
        The rewritten source.

    Raises:
        SyntaxError: If ``text`` is not valid Python.
    """
    tree = ast.parse(text)
    assigns = _assignments(tree)
    by_name: dict[str, list[ast.expr]] = {}
    for name, value, _node in assigns:
        by_name.setdefault(name, []).append(value)

    targets: dict[int, tuple[ast.expr, Any]] = {}

    def _resolve_literals(name: str, seen: set[str]) -> list[ast.expr]:
        nodes: list[ast.expr] = []
        for value in by_name.get(name, []):
            if isinstance(value, ast.Name):
                if value.id in seen or value.id in values:
                    continue
                nodes.extend(_resolve_literals(value.id, seen | {value.id}))
            elif isinstance(value, ast.Constant):
                nodes.append(value)
        return nodes

    appended: list[str] = []
    for name, new_value in values.items():
        if name not in by_name:
            if name == "cdm":
                appended.append(f"cdm = {new_value!r}\n")
            continue
        for node in _resolve_literals(name, {name}):
            targets[id(node)] = (node, new_value)

    offsets = _line_offsets(text)
    edits: list[tuple[int, int, str]] = []
    for node, new_value in targets.values():
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[(node.end_lineno or node.lineno) - 1] + (node.end_col_offset or 0)
        edits.append((start, end, _quote_like(text[start:end], new_value)))
    result = text
    for start, end, replacement in sorted(edits, reverse=True):
        result = result[:start] + replacement + result[end:]
    if appended:
        if result and not result.endswith("\n"):
            result += "\n"
        result += "".join(appended)
    return result


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.tools.pipeline_identity is a library module.")
