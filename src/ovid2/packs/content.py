"""Domain-pack content iteration (tier-neutral).

One place that walks the core YAML trees **and** every active pack's
content directories, attributing each document to its pack and enforcing
the namespace rule (a pack's check ids, package ids and template ids
start with ``<pack_id>.``). Both the executor registries
(:mod:`ovid2.transformations.quality.registry`) and the web tier
(:mod:`ovid2.web.services.health_report_svc`) read through here, so the
catalogue the browser shows is the catalogue the runner executes.

Imports only :mod:`yaml` and :mod:`ovid2.packs`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from ovid2.packs.discovery import active_packs, pack_dirs, pack_for_check, pack_for_member

logger = logging.getLogger(__name__)

__all__ = [
    "CORE_PARTS",
    "ContentDoc",
    "core_health_dir",
    "iter_check_docs",
    "iter_package_docs",
    "iter_template_dirs",
    "part_labels",
    "part_order",
]

#: Core health-check buckets, in report order.
CORE_PARTS: tuple[str, ...] = (
    "part1",
    "part2",
    "part3",
    "part4",
    "mapping",
    "freshness",
    "integrity",
    "completeness",
    "semantic",
)


@dataclass(frozen=True)
class ContentDoc:
    """One YAML document plus its provenance.

    Attributes:
        pack: Owning pack id (blank = core content no pack claims).
        path: Source file.
        raw: Parsed mapping.
    """

    pack: str
    path: Path
    raw: dict[str, Any]


def core_health_dir(kind: str) -> Path:
    """Return the packaged ``resources/health/<kind>/`` directory."""
    with resources.as_file(resources.files("ovid2.resources").joinpath("health", kind)) as path:
        return Path(path)


def _read_dir(directory: Path) -> Iterator[tuple[Path, dict[str, Any]]]:
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.yaml")):
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("packs: %s unreadable: %s", path, exc)
            continue
        if isinstance(raw, dict):
            yield path, raw
        else:
            logger.warning("packs: %s: top-level YAML must be a mapping", path)


def _iter(kind: str, key: str, id_field: str, owner_of: Any) -> list[ContentDoc]:
    out: list[ContentDoc] = []
    seen: dict[str, str] = {}
    for path, raw in _read_dir(core_health_dir(kind)):
        doc_id = str(raw.get(id_field, ""))
        out.append(ContentDoc(owner_of(doc_id), path, raw))
        if doc_id:
            seen[doc_id] = "core"
    for info, directory in pack_dirs(key):
        for path, raw in _read_dir(directory):
            doc_id = str(raw.get(id_field, ""))
            if not doc_id.startswith(f"{info.id}."):
                logger.warning(
                    "packs: %s: %s %r must start with %r — skipped",
                    path,
                    id_field,
                    doc_id,
                    f"{info.id}.",
                )
                continue
            if doc_id in seen:
                logger.warning(
                    "packs: %s: %s %r already provided by %s — skipped",
                    path,
                    id_field,
                    doc_id,
                    seen[doc_id],
                )
                continue
            seen[doc_id] = info.id
            out.append(ContentDoc(info.id, path, raw))
    return out


def iter_check_docs() -> list[ContentDoc]:
    """Return every health-check spec document (core first, then packs)."""
    return _iter("checks", "checks_dir", "id", pack_for_check)


def iter_package_docs() -> list[ContentDoc]:
    """Return every report-cycle package document (core first, then packs)."""
    return _iter("packages", "packages_dir", "id", lambda i: pack_for_member("packages", i))


def iter_template_dirs() -> list[tuple[str, Path]]:
    """Return ``(pack_id, templates_root)`` for every active pack with templates."""
    return [(info.id, path) for info, path in pack_dirs("report_templates_dir")]


def part_order() -> dict[str, int]:
    """Return ``part -> rank``: core parts, then pack parts in discovery order."""
    order = {p: i for i, p in enumerate(CORE_PARTS)}
    for info in active_packs():
        if info.builtin or info.manifest is None:
            continue
        for part_id, _label in info.manifest.parts:
            order.setdefault(part_id, len(order))
    return order


def part_labels() -> dict[str, str]:
    """Return ``part -> label`` for pack-declared parts (core parts are self-describing)."""
    out: dict[str, str] = {}
    for info in active_packs():
        if info.builtin or info.manifest is None:
            continue
        for part_id, label in info.manifest.parts:
            out.setdefault(part_id, label)
    return out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.packs.content is a library module.")
