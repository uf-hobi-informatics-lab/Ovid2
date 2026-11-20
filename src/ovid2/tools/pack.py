"""``ovid2 tools pack`` — inspect, validate, scaffold and enable domain packs.

Subcommands::

    ovid2 tools pack list                 # every discovered pack + state (JSON)
    ovid2 tools pack show <id>            # one pack + what it contributes (JSON)
    ovid2 tools pack validate <path>      # lint a pack directory (exit 1 on errors)
    ovid2 tools pack scaffold <id> --out-dir <dir> [--domain …] [--python]
    ovid2 tools pack enable <id>          # add to $XDG_CONFIG_HOME/ovid2/packs.yaml
    ovid2 tools pack disable <id>

``validate`` checks the manifest against ``pack.v1.json``, every content
file against its schema (``health_check.v1``, ``health_package.v1``,
``semantic_handle.v1``, ``semantic_bindings.v1``, ``report_layout.v1``
and the C1 CDM schemas), the namespace rule (every name the pack adds
starts with its id), that each check's operator is registered (core +
the pack's own operators module when installed), and that every
``remediation`` fixer / transform resolves.

ADR: the adr-domain-packs design note.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

__all__ = ["register", "scaffold_pack", "validate_pack_dir"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _schema(name: str) -> dict[str, Any]:
    from importlib import resources

    ref = resources.files("ovid2.resources.schemas") / f"{name}.json"
    with ref.open("r", encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
    return data


def _yaml_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.yaml")) if directory.is_dir() else []


def _load(path: Path, errors: list[str]) -> dict[str, Any] | None:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        errors.append(f"{path}: {exc}")
        return None
    if not isinstance(raw, dict):
        errors.append(f"{path}: top-level YAML must be a mapping")
        return None
    return raw


def _check(raw: dict[str, Any], schema: dict[str, Any], path: Path, errors: list[str]) -> bool:
    import jsonschema

    try:
        jsonschema.validate(raw, schema)
    except jsonschema.ValidationError as exc:
        where = "/".join(str(p) for p in exc.absolute_path) or "(root)"
        errors.append(f"{path}: {where}: {exc.message}")
        return False
    return True


def validate_pack_dir(root: Path, *, check_operators: bool = True) -> dict[str, Any]:
    """Lint a pack directory and return ``{ok, pack, errors, warnings, counts}``.

    Args:
        root: The pack directory (holding ``pack.yaml``).
        check_operators: Also resolve every check's ``operator`` against
            the executor registry (imports pipeline code; off for callers
            that must stay tier-neutral).
    """
    from ovid2.packs.manifest import PackManifestError, load_manifest

    errors: list[str] = []
    warnings: list[str] = []
    counts: dict[str, int] = {}
    try:
        manifest = load_manifest(root)
    except PackManifestError as exc:
        return {"ok": False, "pack": None, "errors": [str(exc)], "warnings": [], "counts": {}}
    pid = manifest.id
    prov = manifest.provides
    ns = f"{pid}."

    if prov.get("members"):
        warnings.append("provides.members is only meaningful for built-in packs")

    # CDMs (C1 layout).
    cdm_schema = _schema("cdm.v1")
    table_schema = _schema("cdm_table.v1")
    valueset_schema = _schema("cdm_valueset.v1")
    for cdm in manifest.cdms:
        if cdm != pid and not cdm.startswith(f"{pid}_"):
            errors.append(f"CDM {cdm!r} must be named {pid!r} or {pid}_<name>")
        cdm_dir = root / "cdms" / cdm
        cdm_yaml = cdm_dir / "cdm.yaml"
        if not cdm_yaml.is_file():
            errors.append(f"CDM {cdm!r}: missing {cdm_yaml}")
            continue
        raw = _load(cdm_yaml, errors)
        if raw is not None and _check(raw, cdm_schema, cdm_yaml, errors):
            if raw.get("name") != cdm:
                errors.append(f"{cdm_yaml}: name {raw.get('name')!r} != {cdm!r}")
            for table in raw.get("tables") or []:
                tpath = cdm_dir / "tables" / f"{str(table).lower()}.yaml"
                if not tpath.is_file():
                    errors.append(f"CDM {cdm!r}: table {table!r} has no {tpath.name}")
        for tpath in _yaml_files(cdm_dir / "tables"):
            traw = _load(tpath, errors)
            if traw is not None:
                _check(traw, table_schema, tpath, errors)
        for vpath in _yaml_files(cdm_dir / "valuesets"):
            vraw = _load(vpath, errors)
            if vraw is not None:
                _check(vraw, valueset_schema, vpath, errors)
        counts["cdms"] = counts.get("cdms", 0) + 1

    part_ids = {p for p, _ in manifest.parts}
    for p in part_ids:
        if p != pid and not p.startswith(f"{pid}_"):
            errors.append(f"part {p!r} must be named {pid}_<name>")
    from ovid2.packs.content import CORE_PARTS

    known_parts = set(CORE_PARTS) | part_ids

    # Health checks.
    check_ids: set[str] = set()
    operators: set[str] = set()
    remediation_fixers: set[str] = set()
    remediation_transforms: set[str] = set()
    check_schema = _schema("health_check.v1")
    checks_rel = prov.get("checks_dir")
    if checks_rel:
        for path in _yaml_files(root / checks_rel):
            raw = _load(path, errors)
            if raw is None or not _check(raw, check_schema, path, errors):
                continue
            cid = str(raw["id"])
            if not cid.startswith(ns):
                errors.append(f"{path}: check id {cid!r} must start with {ns!r}")
            if cid in check_ids:
                errors.append(f"{path}: duplicate check id {cid!r}")
            check_ids.add(cid)
            if raw["part"] not in known_parts:
                errors.append(
                    f"{path}: part {raw['part']!r} is neither a core part nor declared in "
                    "provides.parts"
                )
            operators.add(str(raw["operator"]))
            rem = raw.get("remediation") or {}
            remediation_fixers.update(str(f) for f in rem.get("fixers") or [])
            remediation_transforms.update(str(t) for t in rem.get("transforms") or [])
        counts["checks"] = len(check_ids)

    # Report-cycle packages.
    pkg_schema = _schema("health_package.v1")
    packages_rel = prov.get("packages_dir")
    if packages_rel:
        n = 0
        for path in _yaml_files(root / packages_rel):
            raw = _load(path, errors)
            if raw is None or not _check(raw, pkg_schema, path, errors):
                continue
            n += 1
            if not str(raw["id"]).startswith(ns):
                errors.append(f"{path}: package id {raw['id']!r} must start with {ns!r}")
            from ovid2.packs.content import iter_check_docs

            core_ids = {str(d.raw.get("id")) for d in iter_check_docs()}
            for ref in raw["checks"]:
                if str(ref) not in check_ids and str(ref) not in core_ids:
                    errors.append(f"{path}: package references unknown check {ref!r}")
        counts["packages"] = n

    # Vocabularies + bindings.
    vocab_schema = _schema("semantic_handle.v1")
    vocab_rel = prov.get("vocabularies_dir")
    if vocab_rel:
        n = 0
        for path in _yaml_files(root / vocab_rel):
            raw = _load(path, errors)
            if raw is None or not _check(raw, vocab_schema, path, errors):
                continue
            n += 1
            if not str(raw["name"]).startswith(ns):
                errors.append(f"{path}: vocabulary {raw['name']!r} must start with {ns!r}")
        counts["vocabularies"] = n
    bind_schema = _schema("semantic_bindings.v1")
    bind_rel = prov.get("bindings_dir")
    if bind_rel:
        n = 0
        for path in _yaml_files(root / bind_rel):
            raw = _load(path, errors)
            if raw is None or not _check(raw, bind_schema, path, errors):
                continue
            n += 1
        counts["bindings"] = n

    # Report templates.
    tmpl_rel = prov.get("report_templates_dir")
    if tmpl_rel:
        layout_schema = _schema("report_layout.v1")
        from ovid2.reports.sources import ADAPTERS

        n = 0
        tdir = root / tmpl_rel
        for kind_dir in sorted(p for p in tdir.iterdir() if p.is_dir()) if tdir.is_dir() else []:
            if kind_dir.name not in ADAPTERS:
                errors.append(
                    f"{kind_dir}: unknown report-source kind {kind_dir.name!r} "
                    f"(known: {', '.join(sorted(ADAPTERS))})"
                )
                continue
            for path in _yaml_files(kind_dir):
                raw = _load(path, errors)
                if raw is None:
                    continue
                if "name" not in raw or not isinstance(raw.get("layout"), dict):
                    errors.append(f"{path}: template needs `name` and a `layout` mapping")
                    continue
                if _check(raw["layout"], layout_schema, path, errors):
                    n += 1
        counts["report_templates"] = n

    # Fixers (metadata only — the code is checked on import by the registry).
    for name in manifest.fixers:
        if not name.startswith(f"{pid}_"):
            errors.append(f"fixer {name!r} must be named {pid}_<name>")
    if manifest.fixers and not prov.get("fixers_module"):
        errors.append("provides.fixers is set but provides.fixers_module is not")

    # Cross-registry resolution (executor imports).
    if check_operators and (operators or remediation_fixers or remediation_transforms):
        from ovid2.config.fixers_catalogue import FIXER_CATALOGUE
        from ovid2.config.transforms_catalogue import transform_names
        from ovid2.transformations.quality.ops import operator_names

        known_ops = operator_names()
        for op in sorted(operators - known_ops):
            if op.startswith(f"{pid}_") and prov.get("operators_module"):
                warnings.append(
                    f"operator {op!r} is not registered yet — enable the pack "
                    "(and install it) to load its operators_module"
                )
            else:
                errors.append(f"check operator {op!r} is not registered")
        known_fixers = set(FIXER_CATALOGUE) | set(manifest.fixers)
        for f in sorted(remediation_fixers - known_fixers):
            errors.append(f"remediation fixer {f!r} is not a registered fixer")
        for t in sorted(remediation_transforms - set(transform_names())):
            errors.append(f"remediation transform {t!r} is not a registered transform op")

    return {
        "ok": not errors,
        "pack": manifest.as_dict(),
        "errors": errors,
        "warnings": warnings,
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# Scaffold
# ---------------------------------------------------------------------------


def scaffold_pack(pack_id: str, out_dir: Path, *, domain: str = "", python: bool = False) -> Path:
    """Write a minimal, valid pack skeleton and return its root.

    The skeleton has one CDM (``<id>``, one table), one health check and
    one report-cycle package. With ``python=True`` it is laid out as an
    installable distribution (``pyproject.toml`` + import package with the
    ``ovid2.packs`` entry point and an ``operators.py`` stub).

    Raises:
        FileExistsError: When the target directory already exists.
        ValueError: When ``pack_id`` is not a valid pack id.
    """
    import re

    if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", pack_id):
        raise ValueError(f"invalid pack id {pack_id!r} (lower-case, digits, _; 2-41 chars)")
    target = out_dir / (f"ovid2-pack-{pack_id.replace('_', '-')}" if python else pack_id)
    if target.exists():
        raise FileExistsError(str(target))
    root = target / f"ovid2_pack_{pack_id}" if python else target
    files: dict[Path, str] = {}
    provides: dict[str, Any] = {
        "cdms": [pack_id],
        "checks_dir": "checks",
        "packages_dir": "packages",
    }
    if python:
        provides["operators_module"] = "operators"
    manifest = {
        "v": 1,
        "id": pack_id,
        "version": "0.1.0",
        "label": pack_id.replace("_", " ").title(),
        "domain": domain or "example",
        "description": f"Domain pack {pack_id} (scaffolded — edit me).",
        "requires_ovid2": ">=0.1",
        "provides": provides,
    }
    files[root / "pack.yaml"] = yaml.safe_dump(manifest, sort_keys=False)
    files[root / "cdms" / pack_id / "cdm.yaml"] = yaml.safe_dump(
        {
            "v": 1,
            "name": pack_id,
            "version": "0.1",
            "tables": ["subject"],
            "reference_tables": [],
            "flavour_of_null": ["", None],
            "id_columns": {"patient": {"column": "SUBJECT_ID", "width": 64}},
        },
        sort_keys=False,
    )
    files[root / "cdms" / pack_id / "tables" / "subject.yaml"] = yaml.safe_dump(
        {
            "v": 1,
            "name": "subject",
            "composite_key": ["SUBJECT_ID"],
            "fields": [
                {"name": "SUBJECT_ID", "rdbms_type": "RDBMS Text(64)", "order": 1},
                {"name": "BIRTH_DATE", "rdbms_type": "RDBMS Date", "order": 2},
            ],
        },
        sort_keys=False,
    )
    files[root / "checks" / "subject_present.yaml"] = yaml.safe_dump(
        {
            "id": f"{pack_id}.subject_present",
            "name": "SUBJECT_PRESENT",
            "part": "part1",
            "description": "The SUBJECT table is present",
            "operator": "table_present",
            "source_tables": ["SUBJECT"],
            "severity": "blocker",
            "applies_to_cdms": [pack_id],
            "remediation": {"hint": "Deliver the SUBJECT table in the extract."},
        },
        sort_keys=False,
    )
    files[root / "packages" / "baseline.yaml"] = yaml.safe_dump(
        {
            "id": f"{pack_id}.baseline",
            "label": "Baseline",
            "released": "2026-01-01",
            "checks": [f"{pack_id}.subject_present"],
        },
        sort_keys=False,
    )
    if python:
        files[root / "__init__.py"] = f'"""Ovid2 domain pack {pack_id}."""\n'
        files[root / "operators.py"] = (
            f'"""Health-check operators for the {pack_id} pack (executor tier).\n\n'
            "Operator names must start with the pack id. Import column functions\n"
            'from ``ovid2.core.engines.dsl`` (never pyspark) so checks run on every engine.\n"""\n\n'
            "from __future__ import annotations\n\n"
            "from typing import Any\n\n"
            "from ovid2.transformations.quality.ops import OpContext, _mk, operator\n"
            "from ovid2.transformations.quality.results import MetricRow\n\n\n"
            f'@operator("{pack_id}_row_count")\n'
            f"def _row_count(ctx: OpContext, spec: Any) -> list[MetricRow]:\n"
            '    """Emit ROW_COUNT for each present source table."""\n'
            "    rows: list[MetricRow] = []\n"
            "    for table in spec.source_tables:\n"
            "        if ctx.has(table):\n"
            "            n = ctx.rows(ctx.table(table))\n"
            '            rows.append(_mk(spec, source_table=table, metric="ROW_COUNT", '
            "value_num=float(n)))\n"
            "    return rows\n"
        )
        files[target / "pyproject.toml"] = (
            "[build-system]\n"
            'requires = ["hatchling>=1.24"]\n'
            'build-backend = "hatchling.build"\n\n'
            "[project]\n"
            f'name = "ovid2-pack-{pack_id.replace("_", "-")}"\n'
            'version = "0.1.0"\n'
            f'description = "Ovid2 domain pack {pack_id}"\n'
            'requires-python = ">=3.11"\n'
            'dependencies = ["ovid2>=0.1"]\n\n'
            '[project.entry-points."ovid2.packs"]\n'
            f'{pack_id} = "ovid2_pack_{pack_id}"\n\n'
            "[tool.hatch.build.targets.wheel]\n"
            f'packages = ["ovid2_pack_{pack_id}"]\n'
        )
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o755)  # noqa: S103 - directory mode per AGENTS.md (0o755 dirs)
        path.write_text(text, encoding="utf-8")
        os.chmod(path, 0o644)
    return target


# ---------------------------------------------------------------------------
# Activation file
# ---------------------------------------------------------------------------


def _set_enabled(pack_id: str, enabled: bool) -> Path:
    from ovid2.config.paths import config_dir

    path = config_dir() / "packs.yaml"
    raw: dict[str, Any] = {}
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            raw = loaded
    ids = [str(x) for x in raw.get("enabled") or []]
    if enabled and pack_id not in ids:
        ids.append(pack_id)
    if not enabled:
        ids = [x for x in ids if x != pack_id]
    raw["enabled"] = ids
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Domain packs enabled on this host (ovid2 tools pack enable/disable).\n"
        + yaml.safe_dump(raw, sort_keys=False),
        encoding="utf-8",
    )
    os.chmod(path, 0o644)
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _emit(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def _list_entry(_args: argparse.Namespace) -> int:
    from ovid2.packs import all_packs

    _emit([p.as_dict() for p in all_packs()])
    return 0


def _show_entry(args: argparse.Namespace) -> int:
    from ovid2.packs import get_pack
    from ovid2.packs.content import iter_check_docs, iter_package_docs

    info = get_pack(args.pack_id)
    if info is None:
        print(f"[ovid2 tools pack] unknown pack {args.pack_id!r}", file=sys.stderr)
        return 1
    out = info.as_dict()
    out["checks"] = [str(d.raw.get("id")) for d in iter_check_docs() if d.pack == info.id]
    out["packages"] = [str(d.raw.get("id")) for d in iter_package_docs() if d.pack == info.id]
    out["root"] = str(info.root)
    _emit(out)
    return 0


def _validate_entry(args: argparse.Namespace) -> int:
    result = validate_pack_dir(Path(args.path).resolve())
    _emit(result)
    return 0 if result["ok"] else 1


def _scaffold_entry(args: argparse.Namespace) -> int:
    try:
        target = scaffold_pack(
            args.pack_id, Path(args.out_dir).resolve(), domain=args.domain, python=args.python
        )
    except (FileExistsError, ValueError) as exc:
        print(f"[ovid2 tools pack] scaffold failed: {exc}", file=sys.stderr)
        return 1
    print(f"[ovid2 tools pack] scaffolded {args.pack_id} at {target}")
    return 0


def _enable_entry(args: argparse.Namespace) -> int:
    path = _set_enabled(args.pack_id, True)
    print(f"[ovid2 tools pack] enabled {args.pack_id} in {path} — restart the web server + worker")
    return 0


def _disable_entry(args: argparse.Namespace) -> int:
    path = _set_enabled(args.pack_id, False)
    print(f"[ovid2 tools pack] disabled {args.pack_id} in {path} — restart the web server + worker")
    return 0


def register(tool_subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``ovid2 tools pack …`` on the ``tools`` subparser."""
    pack = tool_subparsers.add_parser(
        "pack",
        help="Domain packs: list, show, validate, scaffold, enable, disable.",
        description=(
            "Domain packs bundle a CDM, health checks, packages, vocabularies, "
            "report templates and (pip packs only) operators / fixers. Output is JSON."
        ),
    )
    sub = pack.add_subparsers(dest="pack_command", metavar="COMMAND")

    ls = sub.add_parser("list", help="List every discovered pack and its activation state.")
    ls.set_defaults(_entry=_list_entry, _tool="pack.list")

    show = sub.add_parser("show", help="Show one pack and the checks / packages it contributes.")
    show.add_argument("pack_id")
    show.set_defaults(_entry=_show_entry, _tool="pack.show")

    val = sub.add_parser("validate", help="Validate a pack directory (exit 1 on errors).")
    val.add_argument("path", help="Pack directory holding pack.yaml.")
    val.set_defaults(_entry=_validate_entry, _tool="pack.validate")

    scf = sub.add_parser("scaffold", help="Write a minimal, valid pack skeleton.")
    scf.add_argument("pack_id")
    scf.add_argument("--out-dir", default=".", help="Parent directory (default: cwd).")
    scf.add_argument("--domain", default="", help="Domain slug (e.g. clinical_trials).")
    scf.add_argument(
        "--python",
        action="store_true",
        help="Lay out an installable distribution (entry point + operators stub).",
    )
    scf.set_defaults(_entry=_scaffold_entry, _tool="pack.scaffold")

    en = sub.add_parser("enable", help="Enable a pack on this host (packs.yaml).")
    en.add_argument("pack_id")
    en.set_defaults(_entry=_enable_entry, _tool="pack.enable")

    dis = sub.add_parser("disable", help="Disable a pack on this host (packs.yaml).")
    dis.add_argument("pack_id")
    dis.set_defaults(_entry=_disable_entry, _tool="pack.disable")

    def _fallback(_args: argparse.Namespace) -> int:
        pack.print_help(sys.stderr)
        return 2

    pack.set_defaults(_entry=_fallback)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Use `ovid2 tools pack …`.")
