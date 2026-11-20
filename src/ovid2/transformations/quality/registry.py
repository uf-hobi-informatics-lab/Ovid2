"""Declarative data-health check registry.

Loads the YAML check specs under ``src/ovid2/resources/health/checks/``
**plus every active domain pack's** ``checks_dir`` (via the tier-neutral
:mod:`ovid2.packs.content`) into :class:`CheckSpec` objects and exposes
lookup / filter helpers. The
registry is the source of truth for the ``ovid2 health`` runner, matching
Ovid2's "configuration as data" principle (specs are data, operators are
code).

Each spec names an *operator* from :mod:`ovid2.transformations.quality.ops`;
the runner resolves the operator, hands it the spec + engine session, and
collects the metric rows it returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from ovid2.packs import content as _pack_content

_CHECKS_DIR = Path(__file__).resolve().parent.parent.parent / "resources" / "health" / "checks"
_PACKAGES_DIR = Path(__file__).resolve().parent.parent.parent / "resources" / "health" / "packages"

#: Core part order. Pack-declared parts follow (see :func:`part_order`).
_PART_ORDER: dict[str, int] = {p: i for i, p in enumerate(_pack_content.CORE_PARTS)}

#: Valid ``severity`` values, most urgent first.
SEVERITIES: tuple[str, ...] = ("blocker", "warning", "info")


def part_order() -> dict[str, int]:
    """Return ``part -> rank`` (core parts, then active packs' parts)."""
    return _pack_content.part_order()


@dataclass(frozen=True)
class CheckSpec:
    """One declarative health check.

    Mirrors the ``health_check.v1.json`` schema. Immutable; instances are
    cached at module scope.
    """

    id: str
    name: str
    part: str
    description: str
    operator: str
    edc_table: str = ""
    source_tables: tuple[str, ...] = ()
    na_when_empty: tuple[str, ...] = ()
    params: dict[str, Any] = field(default_factory=dict)
    metrics: tuple[str, ...] = ()
    exception_metrics: tuple[str, ...] = ()
    thresholds: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    doc_summary: str = ""
    doc_interpretation: str = ""
    severity: str = "warning"
    applies_to_cdms: tuple[str, ...] = ()
    remediation: dict[str, Any] = field(default_factory=dict)
    pack: str = ""

    @property
    def sort_key(self) -> tuple[int, str]:
        """Deterministic ordering: by part, then by id."""
        return (part_order().get(self.part, 99), self.id)

    def applies_to(self, cdm_name: str) -> bool:
        """Return whether this check is meaningful for ``cdm_name``.

        An empty ``applies_to_cdms`` means any CDM.
        """
        return not self.applies_to_cdms or cdm_name in self.applies_to_cdms


@dataclass(frozen=True)
class CheckPackage:
    """One EDC / Data-Characterization report cycle (e.g. ``20r1``).

    A package groups the health checks that ship in a cycle by listing
    their ids. Packages may share checks — the same check id can appear
    in several packages. Mirrors the ``health_package.v1.json`` schema.
    Immutable; instances are cached at module scope.
    """

    id: str
    label: str
    released: str
    checks: tuple[str, ...]
    description: str = ""
    pack: str = ""


def _package_from_dict(raw: dict[str, Any], *, pack: str = "") -> CheckPackage:
    """Build a :class:`CheckPackage` from a parsed YAML dict."""
    return CheckPackage(
        id=str(raw["id"]),
        label=str(raw["label"]),
        released=str(raw["released"]),
        checks=tuple(str(c) for c in raw["checks"]),
        description=str(raw.get("description", "")),
        pack=pack,
    )


def _spec_from_dict(raw: dict[str, Any], *, pack: str = "") -> CheckSpec:
    """Build a :class:`CheckSpec` from a parsed YAML dict."""
    severity = str(raw.get("severity", "warning"))
    if severity not in SEVERITIES:
        raise ValueError(f"severity {severity!r} not in {SEVERITIES}")
    return CheckSpec(
        id=str(raw["id"]),
        name=str(raw["name"]),
        part=str(raw["part"]),
        description=str(raw["description"]),
        operator=str(raw["operator"]),
        edc_table=str(raw.get("edc_table", "")),
        source_tables=tuple(str(t) for t in raw.get("source_tables", [])),
        na_when_empty=tuple(str(t) for t in raw.get("na_when_empty", [])),
        params=dict(raw.get("params", {})),
        metrics=tuple(str(m) for m in raw.get("metrics", [])),
        exception_metrics=tuple(str(m) for m in raw.get("exception_metrics", [])),
        thresholds=dict(raw.get("thresholds", {})),
        enabled=bool(raw.get("enabled", True)),
        doc_summary=str(raw.get("doc_summary", "")),
        doc_interpretation=str(raw.get("doc_interpretation", "")),
        severity=severity,
        applies_to_cdms=tuple(str(c) for c in raw.get("applies_to_cdms", [])),
        remediation=dict(raw.get("remediation", {})),
        pack=pack,
    )


def spec_sources() -> list[Path]:
    """Return every spec file the registry loads (core + active packs)."""
    return [doc.path for doc in _pack_content.iter_check_docs()]


def package_sources() -> list[Path]:
    """Return every package manifest the registry loads (core + active packs)."""
    return [doc.path for doc in _pack_content.iter_package_docs()]


def clear_cache() -> None:
    """Drop the cached specs / packages (after enabling or editing a pack)."""
    load_all_specs.cache_clear()
    load_all_packages.cache_clear()


@lru_cache(maxsize=1)
def load_all_specs() -> tuple[CheckSpec, ...]:
    """Load and cache every check spec, sorted by part then id.

    Reads the core tree and every active domain pack's ``checks_dir``
    (pack ids must start with ``<pack_id>.``; a duplicate id is skipped
    with a warning). Malformed specs (missing required keys, bad YAML)
    are skipped with a ``ValueError`` re-raised only if *no* spec loads
    — a single bad file should not silently empty the registry.
    """
    specs: list[CheckSpec] = []
    errors: list[str] = []
    if not _CHECKS_DIR.is_dir():
        return ()
    for doc in _pack_content.iter_check_docs():
        try:
            specs.append(_spec_from_dict(doc.raw, pack=doc.pack))
        except (KeyError, ValueError) as exc:
            errors.append(f"{doc.path.name}: {exc}")
    if not specs and errors:
        raise ValueError("No health check specs loaded. Errors: " + "; ".join(errors))
    specs.sort(key=lambda s: s.sort_key)
    return tuple(specs)


def get_spec(check_id: str) -> CheckSpec | None:
    """Return the spec with ``check_id`` (matches ``id`` or ``name``)."""
    for spec in load_all_specs():
        if spec.id == check_id or spec.name == check_id:
            return spec
    return None


@lru_cache(maxsize=1)
def load_all_packages() -> tuple[CheckPackage, ...]:
    """Load and cache every check package, most-recent first.

    Packages are ordered by their ``released`` date descending (newest
    first); ties break on ``id`` descending so ordering is deterministic.
    Malformed manifests are skipped, mirroring :func:`load_all_specs`; a
    single bad file does not empty the registry, but if *no* package
    loads and there were errors a ``ValueError`` is raised.
    """
    packages: list[CheckPackage] = []
    errors: list[str] = []
    if not _PACKAGES_DIR.is_dir():
        return ()
    for doc in _pack_content.iter_package_docs():
        try:
            packages.append(_package_from_dict(doc.raw, pack=doc.pack))
        except (KeyError, ValueError) as exc:
            errors.append(f"{doc.path.name}: {exc}")
    if not packages and errors:
        raise ValueError("No health check packages loaded. Errors: " + "; ".join(errors))
    packages.sort(key=lambda p: (p.released, p.id), reverse=True)
    return tuple(packages)


def get_package(package_id: str) -> CheckPackage | None:
    """Return the package with ``package_id`` (matches ``id``)."""
    for pkg in load_all_packages():
        if pkg.id == package_id:
            return pkg
    return None


def packages() -> list[str]:
    """Return the distinct package ids, most-recent first."""
    return [pkg.id for pkg in load_all_packages()]


def latest_package() -> str | None:
    """Return the newest package id (by ``released`` date), or ``None``."""
    pkgs = load_all_packages()
    return pkgs[0].id if pkgs else None


def select_specs(
    *,
    parts: list[str] | None = None,
    checks: list[str] | None = None,
    packages: list[str] | None = None,
    include_disabled: bool = False,
) -> list[CheckSpec]:
    """Return the enabled specs matching the requested selection.

    Selection precedence (highest first):

    1. ``checks`` — explicit check ids/names; the exact set is returned.
    2. ``packages`` — expands to the union of the member check ids of the
       named cycle-packages (e.g. ``20r1``); duplicates across packages
       collapse (packages may share checks).
    3. ``parts`` — the report-nav buckets (``part1`` … ``semantic``).

    A lower-precedence argument is ignored when a higher one is given.
    ``None``/empty everywhere means all enabled specs.

    Args:
        parts: Restrict to these parts. Ignored when ``checks`` or
            ``packages`` is given.
        checks: Restrict to these explicit check ids/names. Overrides
            ``packages`` and ``parts``.
        packages: Restrict to the union of these cycle-packages' checks.
            Overrides ``parts``. Unknown package ids contribute nothing.
        include_disabled: When ``True``, ``enabled: false`` specs are kept.

    Returns:
        Specs sorted by part then id.
    """
    part_set = {p.strip().lower() for p in parts} if parts else None
    check_set = {c.strip() for c in checks} if checks else None
    package_check_set: set[str] | None = None
    if not check_set and packages:
        package_check_set = set()
        for pkg_id in packages:
            pkg = get_package(pkg_id.strip())
            if pkg is not None:
                package_check_set.update(pkg.checks)
    out: list[CheckSpec] = []
    for spec in load_all_specs():
        if not spec.enabled and not include_disabled:
            continue
        if check_set is not None:
            # Explicit check selection overrides every other filter.
            if spec.id not in check_set and spec.name not in check_set:
                continue
            out.append(spec)
            continue
        if package_check_set is not None:
            # Package selection overrides the part filter.
            if spec.id not in package_check_set and spec.name not in package_check_set:
                continue
            out.append(spec)
            continue
        if part_set is not None and spec.part.lower() not in part_set:
            continue
        out.append(spec)
    return out


def parts() -> list[str]:
    """Return the distinct parts present in the registry, in canonical order."""
    seen = {spec.part for spec in load_all_specs()}
    order = part_order()
    return sorted(seen, key=lambda p: order.get(p, 99))


__all__ = [
    "SEVERITIES",
    "CheckPackage",
    "CheckSpec",
    "clear_cache",
    "get_package",
    "get_spec",
    "latest_package",
    "load_all_packages",
    "load_all_specs",
    "package_sources",
    "packages",
    "part_order",
    "parts",
    "select_specs",
    "spec_sources",
]
