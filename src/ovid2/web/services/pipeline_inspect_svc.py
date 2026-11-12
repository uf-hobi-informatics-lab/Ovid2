"""Static inspection of a pipeline package (or template) — web tier.

Answers, *without executing pipeline code*, the questions the
create-pipeline wizard and the pipeline builder ask:

* **Tables** — for every CDM table: whether the package formats it
  (YAML adapter / custom class / none), the source file pattern(s), the
  auxiliary tables, the mapper (package YAML, CDM default, legacy class),
  the dictionaries and salt keys the mapper uses, the fix chain and the
  assigned transform chains.
* **Identity** — every identity constant the package declares
  (:data:`ovid2.tools.pipeline_identity.IDENTITY_FIELDS`), with its
  current value and the default a clone would get.
* **Requirements** — CDM, ETL profile, required engine family (package
  ``REQUIRES_FAMILY`` or any custom formatter's ``requires_family``).
* **Bindings** — every name that must match configuration elsewhere,
  each checked: ``ok`` / ``warn`` / ``error`` with a fix hint
  (:func:`check_bindings`).

Reads YAML with :mod:`yaml`, Python with :mod:`ast`; never imports
``ovid2.core`` / ``formatters`` / ``mappers`` / ``transformations`` /
``semantic`` (``tests/contract/test_web_no_pipeline_imports.py``).
"""

from __future__ import annotations

import ast
import importlib
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

__all__ = [
    "Binding",
    "PackageInspection",
    "TableInspection",
    "bindings_as_dicts",
    "check_bindings",
    "inspect_package",
    "inspect_template",
]


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class TableInspection:
    """What a package does for one CDM table."""

    name: str
    formatter: str = ""  # "" | "yaml" | "custom" | "python"
    adapter: str = ""
    formatter_class: str = ""
    inputs: list[str] = field(default_factory=list)
    auxiliary_tables: list[str] = field(default_factory=list)
    mapper: str = ""  # "" | "yaml" | "cdm_default" | "legacy_class"
    mapper_ops: list[str] = field(default_factory=list)
    dictionaries: list[str] = field(default_factory=list)
    salt_keys: list[str] = field(default_factory=list)
    fixers: list[str] = field(default_factory=list)
    transforms: list[str] = field(default_factory=list)
    requires_family: str = ""
    is_reference: bool = False

    @property
    def configured(self) -> bool:
        """Return ``True`` when ``--tables all`` would run this table."""
        return bool(self.formatter)


@dataclass
class Binding:
    """One identifier that must match configuration elsewhere."""

    id: str
    category: str
    label: str
    value: str
    must_match: str
    status: str = "ok"  # ok | warn | error | info
    detail: str = ""
    where: str = ""


@dataclass
class PackageInspection:
    """Full static inspection of one package."""

    pipeline: str
    cdm: str
    profile: str
    requires_family: str
    tables: list[TableInspection]
    identity: list[dict[str, Any]]
    multi_table_fixes: list[str]
    fixer_source: str
    bindings: list[Binding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict (tables carry ``configured``)."""
        out = asdict(self)
        for t_out, t in zip(out["tables"], self.tables, strict=True):
            t_out["configured"] = t.configured
        out["summary"] = {
            "tables": len(self.tables),
            "configured": sum(1 for t in self.tables if t.configured),
            "with_mapper": sum(1 for t in self.tables if t.mapper),
            "with_fixers": sum(1 for t in self.tables if t.fixers),
            "with_transforms": sum(1 for t in self.tables if t.transforms),
            "errors": sum(1 for b in self.bindings if b.status == "error"),
            "warnings": sum(1 for b in self.bindings if b.status == "warn"),
        }
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _package_dir(pipeline: str) -> Path | None:
    try:
        pkg = importlib.import_module(f"ovid2_pipelines.{pipeline}")
    except ModuleNotFoundError:
        return None
    for base in getattr(pkg, "__path__", []) or []:
        if Path(base).is_dir():
            return Path(base)
    return None


def _safe_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _parse(text: str) -> ast.Module | None:
    """``ast.parse`` with verbatim-legacy ``SyntaxWarning`` noise silenced."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        try:
            return ast.parse(text)
        except SyntaxError:
            return None


def _class_attr(path: Path, attr: str) -> str:
    """Return the first class-level string ``attr = '...'`` in a module."""
    try:
        tree = _parse(path.read_text(encoding="utf-8"))
    except OSError:
        return ""
    if tree is None:
        return ""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if (
                isinstance(target, ast.Name)
                and target.id == attr
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                return node.value.value
    return ""


def _module_path_for_class(class_path: str) -> Path | None:
    """Locate the ``.py`` file of a dotted ``pkg.mod.Class`` path statically."""
    module = class_path.rsplit(".", 1)[0] if "." in class_path else class_path
    parts = module.split(".")
    if not parts or parts[0] != "ovid2_pipelines" or len(parts) < 3:
        return None
    base = _package_dir(parts[1])
    if base is None:
        return None
    candidate = base.joinpath(*parts[2:]).with_suffix(".py")
    return candidate if candidate.is_file() else None


def _walk_dict_refs(obj: Any, key: str, out: set[str]) -> None:
    """Collect every string value under ``key`` anywhere in ``obj``."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, str):
                out.add(v)
            _walk_dict_refs(v, key, out)
    elif isinstance(obj, list):
        for item in obj:
            _walk_dict_refs(item, key, out)


def _dictionary_names(pkg: Path) -> set[str]:
    """Return the module-level names defined in ``dictionaries.py`` (ast)."""
    path = pkg / "dictionaries.py"
    if not path.is_file():
        return set()
    tree = _parse(path.read_text(encoding="utf-8"))
    if tree is None:
        return set()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _cdm_mapper_dir(cdm: str) -> Path | None:
    import ovid2

    path = Path(ovid2.__file__).resolve().parent / "resources" / "cdms" / cdm / "mappers"
    return path if path.is_dir() else None


def _formatter_info(pkg: Path, pipeline: str, table: str) -> dict[str, Any]:
    """Return formatter facts for ``table`` (mirrors ``core.loaders.has_formatter``)."""
    info: dict[str, Any] = {
        "formatter": "",
        "adapter": "",
        "formatter_class": "",
        "inputs": [],
        "auxiliary_tables": [],
        "requires_family": "",
    }
    fdir = pkg / "formatters"
    if (fdir / f"{table}.py").is_file():
        info["formatter"] = "python"
        return info
    for suffix in (".yaml", ".yml"):
        path = fdir / f"{table}{suffix}"
        if path.is_file():
            raw = _safe_yaml(path)
            info["formatter"] = "yaml"
            info["adapter"] = str(raw.get("adapter", ""))
            inputs: list[str] = []
            if raw.get("input_file_name"):
                inputs.append(str(raw["input_file_name"]))
            for source in (raw.get("sources") or {}).values():
                if isinstance(source, dict) and source.get("glob"):
                    inputs.append(str(source["glob"]))
            info["inputs"] = list(dict.fromkeys(inputs))
            info["auxiliary_tables"] = [str(a) for a in raw.get("auxiliary_tables") or []]
            class_path = str(raw.get("class", "") or "")
            info["formatter_class"] = class_path
            if class_path:
                module_path = _module_path_for_class(class_path)
                if module_path is not None:
                    if not info["inputs"]:
                        name = _class_attr(module_path, "input_file_name")
                        if name:
                            info["inputs"] = [name]
                    info["requires_family"] = _class_attr(module_path, "requires_family")
            return info
    custom = pkg / "formatters_custom" / f"{table}_formatter.py"
    if custom.is_file():
        info["formatter"] = "custom"
        info["formatter_class"] = f"ovid2_pipelines.{pipeline}.formatters_custom.{table}_formatter"
        name = _class_attr(custom, "input_file_name")
        info["inputs"] = [name] if name else []
        info["requires_family"] = _class_attr(custom, "requires_family")
    return info


def _mapper_info(
    pkg: Path, cdm: str, table: str, legacy_profile: bool, mappers_type: str
) -> dict[str, Any]:
    info: dict[str, Any] = {"mapper": "", "mapper_ops": [], "dictionaries": [], "salt_keys": []}
    raw: dict[str, Any] = {}
    for suffix in (".yaml", ".yml"):
        path = pkg / "mappers" / f"{table}{suffix}"
        if path.is_file():
            raw = _safe_yaml(path)
            info["mapper"] = "yaml"
            break
    if not raw and legacy_profile:
        cdm_dir = _cdm_mapper_dir(cdm)
        if cdm_dir is not None and (cdm_dir / f"{table}.yaml").is_file():
            raw = _safe_yaml(cdm_dir / f"{table}.yaml")
            info["mapper"] = "cdm_default"
    if not raw and mappers_type:
        info["mapper"] = "legacy_class"
    ops = raw.get("ops") or []
    info["mapper_ops"] = [str(op.get("op", "")) for op in ops if isinstance(op, dict)]
    dicts: set[str] = set()
    _walk_dict_refs(ops, "dict", dicts)
    # ``@provider:fn`` references are computed lookups, not dictionaries.py names.
    info["dictionaries"] = sorted(d for d in dicts if not d.startswith("@"))
    salts = {
        str(op.get("salt_key", "id_salt"))
        for op in ops
        if isinstance(op, dict) and op.get("op") == "hash_id"
    }
    info["salt_keys"] = sorted(salts)
    return info


def _assignment_labels(pipeline: str) -> tuple[dict[str, list[str]], str]:
    """Return ``({table: [labels]}, error)`` for the package's assignments."""
    from ovid2.config.transform_assignments import AssignmentsError, load_assignments

    try:
        parsed = load_assignments(pipeline)
    except (AssignmentsError, yaml.YAMLError) as exc:
        return {}, str(exc)
    return {t: [a.label for a in entries if a.enabled] for t, entries in parsed.items()}, ""


def _settings_text(pkg: Path) -> str:
    path = pkg / "pipeline_settings.py"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


def inspect_package(
    pipeline: str,
    *,
    new_name: str | None = None,
    identity_overrides: dict[str, Any] | None = None,
    engine: str | None = None,
) -> PackageInspection:
    """Statically inspect an installed pipeline package.

    Args:
        pipeline: Package name under ``ovid2_pipelines``.
        new_name: When inspecting a template for cloning, the clone's name —
            identity rows then carry the clone's ``default`` values.
        identity_overrides: Wizard-supplied identity values (checked by
            :func:`check_bindings` and reflected in ``default``).
        engine: Engine name or type to check family compatibility against.

    Raises:
        LookupError: When the package is not installed.
    """
    from ovid2.config.fixer_configs import FixerConfigsError, load_fixer_configs
    from ovid2.tools.pipeline_identity import (
        default_identity,
        identity_fields_for,
        read_settings_constants,
    )

    pkg = _package_dir(pipeline)
    if pkg is None:
        raise LookupError(f"Pipeline package '{pipeline}' is not installed.")
    constants = read_settings_constants(_settings_text(pkg))
    cdm_name = str(constants.get("cdm") or "")
    mappers_type = str(constants.get("mappers_type") or "")
    if not cdm_name:
        from ovid2.cdm.registry import resolve_cdm_for_pipeline

        try:
            cdm_name = resolve_cdm_for_pipeline(pipeline).name
        except ValueError:
            cdm_name = ""
    profile = str(constants.get("ETL_PROFILE") or "default")
    legacy = profile == "legacy_ovid"

    tables: list[str] = []
    reference: frozenset[str] = frozenset()
    if cdm_name:
        from ovid2.cdm.registry import get_cdm

        try:
            handle = get_cdm(cdm_name)
            tables = list(handle.tables())
            reference = handle.reference_tables
        except (KeyError, ValueError):
            tables = []
    try:
        chains, multi, fixer_source = load_fixer_configs(pipeline)
    except FixerConfigsError:
        chains, multi, fixer_source = {}, [], "invalid"
    assigned, _assign_err = _assignment_labels(pipeline)

    rows: list[TableInspection] = []
    for table in tables:
        f = _formatter_info(pkg, pipeline, table)
        m = _mapper_info(pkg, cdm_name, table, legacy, mappers_type)
        rows.append(
            TableInspection(
                name=table,
                formatter=f["formatter"],
                adapter=f["adapter"],
                formatter_class=f["formatter_class"],
                inputs=f["inputs"],
                auxiliary_tables=f["auxiliary_tables"],
                mapper=m["mapper"],
                mapper_ops=m["mapper_ops"],
                dictionaries=m["dictionaries"],
                salt_keys=m["salt_keys"],
                fixers=[str(e.get("fix", "")) for e in chains.get(table, [])],
                transforms=[*assigned.get("*", []), *assigned.get(table, [])],
                requires_family=f["requires_family"],
                is_reference=table in reference,
            )
        )

    requires = str(constants.get("REQUIRES_FAMILY") or "").lower()
    if legacy:
        requires = requires or "spark"

    defaults = default_identity(
        new_name or pipeline, constants, cdm=cdm_name, overrides=identity_overrides
    )
    identity = [
        {
            "key": fld.key,
            "label": fld.label,
            "category": fld.category,
            "must_match": fld.must_match,
            "choices": list(fld.choices),
            "current": constants.get(fld.key, cdm_name if fld.key == "cdm" else None),
            "default": defaults.get(fld.key),
        }
        for fld in identity_fields_for({**constants, "cdm": cdm_name})
    ]

    inspection = PackageInspection(
        pipeline=pipeline,
        cdm=cdm_name,
        profile=profile,
        requires_family=requires,
        tables=rows,
        identity=identity,
        multi_table_fixes=[str(e.get("fix", "")) for e in multi],
        fixer_source=fixer_source,
    )
    inspection.bindings = check_bindings(
        inspection,
        pkg=pkg,
        chains=chains,
        multi=multi,
        constants=constants,
        identity_values=defaults if new_name else None,
        engine=engine,
        clone_name=new_name,
    )
    return inspection


def inspect_template(
    template_id: str,
    *,
    new_name: str | None = None,
    identity_overrides: dict[str, Any] | None = None,
    engine: str | None = None,
) -> dict[str, Any]:
    """Inspect a template's source package for the create wizard.

    Raises:
        LookupError: Unknown / unavailable template.
    """
    from ovid2.tools.templates import get_template

    template = get_template(template_id)
    if template is None:
        raise LookupError(f"Unknown pipeline template '{template_id}'.")
    result = inspect_package(
        template.source_pipeline,
        new_name=new_name or "new_pipeline",
        identity_overrides=identity_overrides,
        engine=engine,
    ).to_dict()
    result["template"] = {
        "id": template.id,
        "label": template.label,
        "description": template.description,
        "source_pipeline": template.source_pipeline,
        "cdm": template.cdm,
    }
    return result


# ---------------------------------------------------------------------------
# Bindings — names that must match configuration elsewhere
# ---------------------------------------------------------------------------


def _secret_keys(name: str) -> set[str] | None:
    """Return the key *names* of ``secrets.d/<name>.json`` (never values)."""
    from ovid2.web.services import secrets_svc

    detail = secrets_svc.get_secret(name)
    return None if detail is None else set(detail.keys)


def _lists_dir() -> Path | None:
    import os

    base = os.environ.get("OVID2_MATERIAL_DIR")
    if not base:
        return None
    path = Path(base) / "lists"
    return path if path.is_dir() else None


def _known_vocabularies() -> set[str]:
    import ovid2

    root = Path(ovid2.__file__).resolve().parent / "resources" / "semantic" / "vocabularies"
    return {p.stem for p in root.glob("*.yaml")}


def _engine_family_for(engine: str) -> str | None:
    from ovid2.config.engines import engine_family, resolve_engine_type

    engine_type = resolve_engine_type(engine) or engine
    return engine_family(engine_type)


def check_bindings(
    inspection: PackageInspection,
    *,
    pkg: Path,
    chains: dict[str, list[dict[str, Any]]],
    multi: list[dict[str, Any]],
    constants: dict[str, Any],
    identity_values: dict[str, Any] | None = None,
    engine: str | None = None,
    clone_name: str | None = None,
) -> list[Binding]:
    """Check every cross-configuration name the package depends on.

    Each :class:`Binding` names the value, what it must match, and a
    status: ``error`` (the run will fail), ``warn`` (the run degrades or
    a value still looks like a template placeholder) or ``ok``.
    """
    from ovid2.cdm.registry import registered_cdms
    from ovid2.config.fixers_catalogue import CANONICAL_KEYS, FIXER_CATALOGUE

    out: list[Binding] = []
    pipeline = clone_name or inspection.pipeline
    values = identity_values or constants
    cdm = str(values.get("cdm") or inspection.cdm)

    # -- CDM -------------------------------------------------------------
    out.append(
        Binding(
            id="cdm",
            category="cdm",
            label="Target CDM",
            value=cdm,
            must_match="ovid2.cdm.registry",
            status="ok" if cdm in registered_cdms() else "error",
            detail="" if cdm in registered_cdms() else f"CDM '{cdm}' is not registered.",
            where="pipeline_settings.py",
        )
    )

    # -- identity placeholders ------------------------------------------
    for row in inspection.identity:
        key = row["key"]
        if key == "cdm":
            continue
        value = values.get(key, row["current"])
        status, detail = "ok", ""
        if value in (None, ""):
            status, detail = "warn", "empty"
        elif (
            clone_name is None
            and isinstance(value, str)
            and ("EXAMPLE" in value.upper() or value.lower() == "example")
        ):
            status, detail = "warn", "still a template placeholder"
        if row["choices"] and value not in row["choices"]:
            status, detail = "error", f"must be one of {', '.join(row['choices'])}"
        out.append(
            Binding(
                id=f"identity.{key}",
                category=row["category"],
                label=row["label"],
                value="" if value is None else str(value),
                must_match=row["must_match"],
                status=status,
                detail=detail,
                where="pipeline_settings.py",
            )
        )
    if "DATAMARTID" in values and "NETWORKID" in values:
        dm, net = str(values.get("DATAMARTID") or ""), str(values.get("NETWORKID") or "")
        if dm and net and not dm.startswith(net):
            out.append(
                Binding(
                    id="identity.DATAMARTID.prefix",
                    category="harvest",
                    label="Datamart id prefix",
                    value=dm,
                    must_match=f"starts with NETWORKID ({net})",
                    status="warn",
                    detail="PCORnet datamart ids are prefixed by the network id.",
                    where="pipeline_settings.py",
                )
            )

    # -- engine family ---------------------------------------------------
    if inspection.requires_family:
        status, detail, value = "info", "", inspection.requires_family
        if engine:
            family = _engine_family_for(engine)
            value = f"{engine} ({family or '?'})"
            if family and family != inspection.requires_family:
                status = "error"
                detail = f"package requires a '{inspection.requires_family}' engine."
            else:
                status = "ok"
        out.append(
            Binding(
                id="engine.family",
                category="engine",
                label="Engine family",
                value=value,
                must_match=f"family '{inspection.requires_family}'",
                status=status,
                detail=detail,
                where="REQUIRES_FAMILY / custom formatter requires_family",
            )
        )

    gated = [t for t in inspection.tables if t.configured and t.requires_family]
    if gated and engine:
        family = _engine_family_for(engine)
        bad = [t.name for t in gated if family and t.requires_family != family]
        if bad:
            out.append(
                Binding(
                    id="engine.tables",
                    category="engine",
                    label="Engine-gated tables",
                    value=", ".join(bad),
                    must_match=f"engine family {gated[0].requires_family}",
                    status="warn",
                    detail=(
                        f"these tables' custom formatters need a different engine family "
                        f"than '{family}' — deselect them or pick a compatible engine"
                    ),
                    where="formatters_custom requires_family",
                )
            )

    # -- legacy vendor folder -------------------------------------------
    if inspection.profile == "legacy_ovid":
        partner = str(constants.get("LEGACY_VENDOR_PARTNER") or inspection.pipeline)
        vendor = pkg.parent.parent / "legacy_ovid_vendor" / "partners" / partner / "fixer_scripts"
        out.append(
            Binding(
                id="legacy.vendor",
                category="fixers",
                label="Vendored legacy fixer scripts",
                value=partner,
                must_match="pipelines/legacy_ovid_vendor/partners/<partner>/fixer_scripts",
                status="ok" if vendor.is_dir() else "error",
                detail="" if vendor.is_dir() else "vendored fixer scripts not found",
                where="LEGACY_VENDOR_PARTNER",
            )
        )

    # -- dictionaries ----------------------------------------------------
    dict_names = _dictionary_names(pkg)
    missing_dicts: dict[str, list[str]] = {}
    for t in inspection.tables:
        for name in t.dictionaries:
            if name not in dict_names:
                missing_dicts.setdefault(name, []).append(t.name)
    used = sorted({n for t in inspection.tables for n in t.dictionaries})
    if used:
        out.append(
            Binding(
                id="dictionaries",
                category="mapper",
                label="Mapper dictionaries",
                value=f"{len(used)} referenced",
                must_match="names defined in dictionaries.py",
                status="warn" if missing_dicts else "ok",
                detail=(
                    "missing: "
                    + ", ".join(f"{n} ({', '.join(ts)})" for n, ts in sorted(missing_dicts.items()))
                    if missing_dicts
                    else ""
                ),
                where="mappers/*.yaml dict: → dictionaries.py",
            )
        )

    # -- salt keys -------------------------------------------------------
    salts = sorted({s for t in inspection.tables for s in t.salt_keys})
    if salts:
        keys = _secret_keys(pipeline)
        missing = [s for s in salts if keys is None or s not in keys]
        out.append(
            Binding(
                id="secrets.salt",
                category="secrets",
                label="hash_id salt keys",
                value=", ".join(salts),
                must_match=f"keys in secrets.d/{pipeline}.json",
                status="error" if missing else "ok",
                detail=(
                    f"missing {', '.join(missing)} — IDs would be hashed without salt"
                    if missing
                    else ""
                ),
                where="mappers/*.yaml hash_id.salt_key",
            )
        )

    # -- fixers ----------------------------------------------------------
    lists_dir = _lists_dir()
    tables = {t.name for t in inspection.tables}
    configured = {t.name for t in inspection.tables if t.configured}

    def _check_fix(where: str, entry: dict[str, Any]) -> None:
        name = str(entry.get("fix", ""))
        meta = FIXER_CATALOGUE.get(name)
        if meta is None:
            out.append(
                Binding(
                    id=f"fix.{where}",
                    category="fixers",
                    label=f"Fix {name}",
                    value=name,
                    must_match="registered common fixer",
                    status="error",
                    detail="unknown fixer",
                    where=where,
                )
            )
            return
        if meta["scope"] not in ("generic", cdm):
            out.append(
                Binding(
                    id=f"fix.{where}.scope",
                    category="fixers",
                    label=f"Fix {name}",
                    value=meta["scope"],
                    must_match=f"generic or {cdm}",
                    status="error",
                    detail=f"fixer is scoped to {meta['scope']}",
                    where=where,
                )
            )
        allowed = set(CANONICAL_KEYS) | set(meta["aliases"])
        for key in entry:
            if key not in allowed:
                out.append(
                    Binding(
                        id=f"fix.{where}.{key}",
                        category="fixers",
                        label=f"Fix {name}",
                        value=key,
                        must_match="canonical key or fixer alias",
                        status="error",
                        detail=f"unrecognised key '{key}'",
                        where=where,
                    )
                )
        positional = {v: k for k, v in meta["aliases"].items()}
        for slot in ("src1", "src2"):
            role = positional.get(slot, "")
            value = entry.get(slot) or entry.get(role)
            if not value:
                continue
            if role == "reference_table":
                ok = value in tables
                status = "ok" if ok and value in configured else ("warn" if ok else "error")
                detail = (
                    ""
                    if status == "ok"
                    else (
                        "reference table is not produced by this pipeline"
                        if ok
                        else f"'{value}' is not a {cdm} table"
                    )
                )
                if status != "ok":
                    out.append(
                        Binding(
                            id=f"fix.{where}.ref",
                            category="fixers",
                            label=f"Fix {name} reference",
                            value=str(value),
                            must_match="a CDM table formatted earlier in the run",
                            status=status,
                            detail=detail,
                            where=where,
                        )
                    )
            elif role in ("list_name", "valid_values_name"):
                exists = lists_dir is not None and (lists_dir / f"{value}.csv").is_file()
                out.append(
                    Binding(
                        id=f"fix.{where}.list",
                        category="fixers",
                        label=f"Fix {name} list",
                        value=str(value),
                        must_match="$OVID2_MATERIAL_DIR/lists/<name>.csv",
                        status="ok" if exists else ("info" if lists_dir is None else "warn"),
                        detail=(
                            ""
                            if exists
                            else (
                                "material dir not visible to the web tier"
                                if lists_dir is None
                                else "list file not found"
                            )
                        ),
                        where=where,
                    )
                )

    for table, entries in chains.items():
        if table not in tables:
            out.append(
                Binding(
                    id=f"fix.{table}",
                    category="fixers",
                    label="Fix chain table",
                    value=table,
                    must_match=f"a {cdm} table",
                    status="error",
                    detail="chain key is not a CDM table",
                    where="fixer_configs",
                )
            )
        for i, entry in enumerate(entries):
            _check_fix(f"{table}[{i}]", entry)
    for i, entry in enumerate(multi):
        _check_fix(f"multi[{i}]", entry)

    # -- formatter class paths / vocabularies ----------------------------
    vocabularies = _known_vocabularies()
    for t in inspection.tables:
        if t.formatter_class and t.formatter == "yaml":
            target = t.formatter_class
            if clone_name and target.startswith(f"ovid2_pipelines.{inspection.pipeline}."):
                target = target.replace(
                    f"ovid2_pipelines.{inspection.pipeline}.", f"ovid2_pipelines.{clone_name}.", 1
                )
            resolvable = _module_path_for_class(t.formatter_class) is not None
            if not resolvable:
                out.append(
                    Binding(
                        id=f"formatter.{t.name}.class",
                        category="formatter",
                        label=f"{t.name} formatter class",
                        value=target,
                        must_match="an importable module in the package",
                        status="error",
                        detail="module not found",
                        where=f"formatters/{t.name}.yaml",
                    )
                )
        path = pkg / "formatters" / f"{t.name}.yaml"
        if t.formatter == "yaml" and path.is_file():
            raw = _safe_yaml(path)
            for join in raw.get("joins") or []:
                if isinstance(join, dict) and join.get("with"):
                    name = str(join["with"])
                    if name not in vocabularies and name not in tables and name != "concept":
                        out.append(
                            Binding(
                                id=f"formatter.{t.name}.join.{name}",
                                category="formatter",
                                label=f"{t.name} join",
                                value=name,
                                must_match="registered vocabulary or shared view",
                                status="warn",
                                detail="not a known vocabulary; must be a view registered at run time",
                                where=f"formatters/{t.name}.yaml",
                            )
                        )

    # -- transforms ------------------------------------------------------
    from ovid2.config.transform_assignments import (
        AssignmentsError,
        load_assignments,
        user_library_dir,
    )

    try:
        assigned = load_assignments(inspection.pipeline)
    except (AssignmentsError, yaml.YAMLError) as exc:
        out.append(
            Binding(
                id="transforms.assignments",
                category="transforms",
                label="Transform assignments",
                value="transforms/assignments.yaml",
                must_match="transform_assignments.v1.json",
                status="error",
                detail=str(exc),
                where="transforms/assignments.yaml",
            )
        )
        assigned = {}
    for table, assigned_entries in assigned.items():
        if table != "*" and table not in tables:
            out.append(
                Binding(
                    id=f"transforms.{table}",
                    category="transforms",
                    label="Transform assignment table",
                    value=table,
                    must_match=f"a {cdm} table or '*'",
                    status="error",
                    detail="not a CDM table",
                    where="transforms/assignments.yaml",
                )
            )
        for assignment in assigned_entries:
            if assignment.spec and not _chain_exists(assignment.spec, pkg, user_library_dir()):
                out.append(
                    Binding(
                        id=f"transforms.{table}.{assignment.spec}",
                        category="transforms",
                        label="Assigned transform chain",
                        value=assignment.spec,
                        must_match="package, user or shipped transform library",
                        status="error",
                        detail="chain not found",
                        where="transforms/assignments.yaml",
                    )
                )
    return out


def _chain_exists(name: str, pkg: Path, user_dir: Path) -> bool:
    import ovid2

    shipped = Path(ovid2.__file__).resolve().parent / "resources" / "transforms"
    return (
        any((d / f"{name}.yaml").is_file() for d in (pkg / "transforms", user_dir, shipped))
        or (pkg / "transforms" / f"{name}.py").is_file()
    )


def bindings_as_dicts(bindings: list[Binding]) -> list[dict[str, Any]]:
    """Return bindings as JSON-safe dicts."""
    return [asdict(b) for b in bindings]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("This module is imported by the ovid2 web routes; it is not a script.")
