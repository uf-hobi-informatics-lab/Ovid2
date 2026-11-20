"""Domain-pack discovery and activation.

Three pack sources, merged in this order:

1. **Built-in** — ``src/ovid2/resources/packs/<id>/pack.yaml``. Describe
   content that ships in the core tree (``provides.members``); always
   active, never contribute extra directories.
2. **Installed** — Python distributions declaring an entry point in the
   ``ovid2.packs`` group. The entry point names an importable *package*
   whose directory holds ``pack.yaml``::

       [project.entry-points."ovid2.packs"]
       acme_trials = "ovid2_pack_acme_trials"

   Only installed packs may ship code (``operators_module`` /
   ``fixers_module``) — installing a wheel is already a code-trust
   decision.
3. **Folder** — YAML-only directories ``<packs_dir>/<id>/pack.yaml``
   (``$OVID2_PACKS_DIR`` or ``$XDG_CONFIG_HOME/ovid2/packs``).

Activation (installing ≠ enabling): built-in packs are always active;
other packs are active only when listed in ``OVID2_PACKS_ENABLED``
(comma-separated ids, or ``*`` for every discovered pack) or in
``<config_dir>/packs.yaml`` (``enabled: [ids]``). A pack whose
``requires_ovid2`` constraint fails, whose id collides, or whose
manifest is invalid is reported with an ``error`` and stays inactive.

Discovery locates the pack's directory **without importing** an
installed pack's code (``importlib.util.find_spec``), so this module is
tier-neutral: the web tier and the manual generator use it freely
(``tests/contract/test_packs_tier_neutral.py``). Importing a pack's
``operators_module`` / ``fixers_module`` happens only in the executor
registries.

ADR: the adr-domain-packs design note.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import metadata, resources
from pathlib import Path
from typing import Any

import yaml

from ovid2.packs.manifest import (
    MANIFEST_FILE,
    PackManifest,
    PackManifestError,
    load_manifest,
    version_satisfies,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ENTRY_POINT_GROUP",
    "ENV_ENABLED",
    "ENV_PACKS_DIR",
    "PackInfo",
    "active_packs",
    "all_packs",
    "builtin_root",
    "clear_cache",
    "folder_packs_dir",
    "get_pack",
    "pack_dirs",
    "pack_for_check",
    "pack_for_fixer",
    "pack_for_member",
]

#: Entry-point group installed packs register under.
ENTRY_POINT_GROUP = "ovid2.packs"
#: Comma-separated ids of enabled packs (``*`` = all discovered).
ENV_ENABLED = "OVID2_PACKS_ENABLED"
#: Override for the folder-pack root.
ENV_PACKS_DIR = "OVID2_PACKS_DIR"

#: Names reserved for the core tree (a non-built-in pack may not take them).
_RESERVED_IDS: frozenset[str] = frozenset({"ovid2", "core", "builtin", "health", "generic"})


@dataclass(frozen=True)
class PackInfo:
    """One discovered pack and its activation state.

    Attributes:
        manifest: The parsed manifest (``None`` when it failed to load).
        root: The pack directory.
        origin: ``builtin`` | ``installed`` | ``folder``.
        distribution: Installed packs: ``<dist> <version>``; else blank.
        module: Installed packs: the import package; else blank.
        enabled: Whether the operator enabled it (always ``True`` for built-ins).
        error: Why the pack is inactive despite being enabled (blank = none).
    """

    manifest: PackManifest | None
    root: Path
    origin: str
    distribution: str = ""
    module: str = ""
    enabled: bool = False
    error: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def id(self) -> str:
        """Pack id (falls back to the directory name for broken manifests)."""
        return self.manifest.id if self.manifest is not None else self.root.name

    @property
    def active(self) -> bool:
        """Whether the pack's content is loaded into the registries."""
        return self.enabled and not self.error and self.manifest is not None

    @property
    def builtin(self) -> bool:
        """Whether the pack ships in the core tree."""
        return self.origin == "builtin"

    @property
    def may_ship_code(self) -> bool:
        """Only installed packs may contribute operators / fixers."""
        return self.origin == "installed"

    def content_dir(self, key: str) -> Path | None:
        """Return the absolute content directory for ``provides.<key>``.

        Built-in packs never return a directory: their content lives in
        the core tree and is loaded by the core loaders.
        """
        if self.builtin or self.manifest is None:
            return None
        rel = self.manifest.dir(key)
        if not rel:
            return None
        path = (self.root / rel).resolve()
        try:
            path.relative_to(self.root.resolve())
        except ValueError:
            return None
        return path if path.is_dir() else None

    def cdm_dir(self, cdm_name: str) -> Path | None:
        """Return ``<root>/cdms/<cdm_name>`` when the pack provides it."""
        if self.builtin or self.manifest is None or cdm_name not in self.manifest.cdms:
            return None
        path = self.root / "cdms" / cdm_name
        return path if path.is_dir() else None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary (web / CLI / MCP)."""
        out: dict[str, Any] = {
            "id": self.id,
            "origin": self.origin,
            "enabled": self.enabled,
            "active": self.active,
            "error": self.error,
            "warnings": list(self.warnings),
            "distribution": self.distribution,
            "module": self.module,
        }
        if self.manifest is not None:
            m = self.manifest
            out.update(
                {
                    "version": m.version,
                    "label": m.label,
                    "description": m.description,
                    "domain": m.domain,
                    "homepage": m.homepage,
                    "requires_ovid2": m.requires_ovid2,
                    "provides": m.as_dict()["provides"],
                }
            )
        return out


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


def builtin_root() -> Path:
    """Return the packaged ``resources/packs/`` directory."""
    with resources.as_file(resources.files("ovid2.resources").joinpath("packs")) as path:
        return Path(path)


def folder_packs_dir() -> Path:
    """Return the folder-pack root (``$OVID2_PACKS_DIR`` or ``<config>/packs``)."""
    override = os.environ.get(ENV_PACKS_DIR)
    if override:
        return Path(override)
    from ovid2.config.paths import config_dir

    return config_dir() / "packs"


def _activation_file() -> Path:
    from ovid2.config.paths import config_dir

    return config_dir() / "packs.yaml"


def _enabled_ids() -> tuple[set[str], bool]:
    """Return ``(enabled ids, all_enabled)`` from env + ``packs.yaml``."""
    ids: set[str] = set()
    star = False
    env = os.environ.get(ENV_ENABLED, "")
    for tok in env.split(","):
        tok = tok.strip()
        if tok == "*":
            star = True
        elif tok:
            ids.add(tok)
    path = _activation_file()
    if path.is_file():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("packs.yaml unreadable at %s: %s", path, exc)
            raw = {}
        enabled = raw.get("enabled") if isinstance(raw, dict) else None
        if isinstance(enabled, list):
            for tok in enabled:
                if str(tok).strip() == "*":
                    star = True
                elif str(tok).strip():
                    ids.add(str(tok).strip())
    return ids, star


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _load(root: Path) -> tuple[PackManifest | None, str]:
    try:
        return load_manifest(root), ""
    except PackManifestError as exc:
        return None, str(exc)


def _discover_builtin() -> list[PackInfo]:
    out: list[PackInfo] = []
    root = builtin_root()
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if not (child / MANIFEST_FILE).is_file():
            continue
        manifest, err = _load(child)
        out.append(PackInfo(manifest, child, "builtin", enabled=True, error=err))
    return out


def _discover_installed() -> list[PackInfo]:
    out: list[PackInfo] = []
    try:
        eps = metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # pragma: no cover - broken site-packages
        logger.warning("packs: entry-point scan failed: %s", exc)
        return out
    for ep in sorted(eps, key=lambda e: e.name):
        dist = ""
        if ep.dist is not None:
            dist = f"{ep.dist.metadata['Name']} {ep.dist.version}"
        module = ep.value.split(":", 1)[0].strip()
        try:
            spec = importlib.util.find_spec(module)
        except (ImportError, ValueError) as exc:
            spec = None
            find_err = str(exc)
        else:
            find_err = ""
        locations = list(spec.submodule_search_locations or []) if spec is not None else []
        if not locations:
            out.append(
                PackInfo(
                    None,
                    Path(ep.name),
                    "installed",
                    distribution=dist,
                    module=module,
                    error=f"entry point {ep.name!r}: {module!r} is not an importable package"
                    + (f" ({find_err})" if find_err else ""),
                )
            )
            continue
        root = Path(locations[0])
        manifest, err = _load(root)
        if manifest is not None and manifest.id != ep.name:
            err = f"entry point name {ep.name!r} does not match pack id {manifest.id!r}"
        out.append(
            PackInfo(manifest, root, "installed", distribution=dist, module=module, error=err)
        )
    return out


def _discover_folder() -> list[PackInfo]:
    out: list[PackInfo] = []
    root = folder_packs_dir()
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not (child / MANIFEST_FILE).is_file():
            continue
        manifest, err = _load(child)
        if manifest is not None and manifest.id != child.name:
            err = f"folder name {child.name!r} does not match pack id {manifest.id!r}"
        out.append(PackInfo(manifest, child, "folder", error=err))
    return out


def _validate(info: PackInfo, version: str) -> tuple[str, tuple[str, ...]]:
    """Return ``(error, warnings)`` for a non-built-in pack."""
    m = info.manifest
    if m is None:
        return info.error, ()
    warnings: list[str] = []
    if m.id in _RESERVED_IDS:
        return f"pack id {m.id!r} is reserved", ()
    try:
        if not version_satisfies(version, m.requires_ovid2):
            return f"requires ovid2 {m.requires_ovid2} (running {version})", ()
    except PackManifestError as exc:
        return str(exc), ()
    for cdm in m.cdms:
        if cdm != m.id and not cdm.startswith(f"{m.id}_"):
            return f"CDM {cdm!r} must be named {m.id!r} or {m.id}_<name>", ()
        if not (info.root / "cdms" / cdm / "cdm.yaml").is_file():
            return f"CDM {cdm!r}: missing cdms/{cdm}/cdm.yaml", ()
    for part_id, _label in m.parts:
        if part_id != m.id and not part_id.startswith(f"{m.id}_"):
            return f"part {part_id!r} must be named {m.id}_<name>", ()
    for fixer in m.fixers:
        if not fixer.startswith(f"{m.id}_"):
            return f"fixer {fixer!r} must be named {m.id}_<name>", ()
    code_keys = [k for k in ("operators_module", "fixers_module") if m.provides.get(k)]
    if code_keys and not info.may_ship_code:
        return (
            f"{', '.join(code_keys)}: only installed (pip) packs may ship code; "
            "folder packs are YAML-only",
            (),
        )
    if m.provides.get("fixers") and not m.provides.get("fixers_module"):
        warnings.append("provides.fixers is set but provides.fixers_module is not")
    if m.provides.get("members"):
        warnings.append("provides.members is only meaningful for built-in packs; ignored")
    return "", tuple(warnings)


@lru_cache(maxsize=1)
def all_packs() -> tuple[PackInfo, ...]:
    """Return every discovered pack (built-in first), with activation state.

    Cached; call :func:`clear_cache` after changing the environment, the
    folder-pack root or the installed distributions.
    """
    from ovid2 import __version__

    enabled_ids, star = _enabled_ids()
    seen: dict[str, PackInfo] = {}
    out: list[PackInfo] = []
    for info in _discover_builtin():
        out.append(info)
        seen[info.id] = info
    for info in _discover_installed() + _discover_folder():
        enabled = star or info.id in enabled_ids
        error, warnings = _validate(info, __version__)
        # A discovery-time error (folder / entry-point name != pack id)
        # wins over validation: never activate a mis-named pack.
        error = info.error or error
        if not error and info.id in seen:
            error = f"pack id {info.id!r} already provided by {seen[info.id].origin} pack"
        resolved = PackInfo(
            info.manifest,
            info.root,
            info.origin,
            distribution=info.distribution,
            module=info.module,
            enabled=enabled,
            error=error,
            warnings=warnings,
        )
        out.append(resolved)
        if not error:
            seen.setdefault(info.id, resolved)
    return tuple(out)


def active_packs() -> tuple[PackInfo, ...]:
    """Return the packs whose content is loaded (built-ins + enabled, valid)."""
    return tuple(p for p in all_packs() if p.active)


def get_pack(pack_id: str) -> PackInfo | None:
    """Return the first discovered pack with ``pack_id`` (active or not)."""
    for info in all_packs():
        if info.id == pack_id:
            return info
    return None


def pack_dirs(key: str) -> list[tuple[PackInfo, Path]]:
    """Return ``(pack, directory)`` for every active pack providing ``key``.

    ``key`` is one of the ``provides.*_dir`` keys (``checks_dir`` …).
    Built-in packs never appear (their content is in the core tree).
    """
    out: list[tuple[PackInfo, Path]] = []
    for info in active_packs():
        path = info.content_dir(key)
        if path is not None:
            out.append((info, path))
    return out


def pack_for_member(kind: str, name: str) -> str:
    """Return the built-in pack that claims core ``name`` of ``kind`` (blank = none).

    ``kind`` is a ``provides.members`` key: ``checks``, ``packages``,
    ``fixers`` or ``vocabularies``.
    """
    return _ownership().get((kind, name), "")


def pack_for_check(check_id: str) -> str:
    """Return the id of the pack that owns ``check_id`` (blank = core, unowned)."""
    return pack_for_member("checks", check_id)


def pack_for_fixer(fixer_name: str) -> str:
    """Return the id of the pack that owns ``fixer_name`` (blank = core, unowned)."""
    if fixer_name in _pack_fixer_owners():
        return _pack_fixer_owners()[fixer_name]
    return pack_for_member("fixers", fixer_name)


@lru_cache(maxsize=1)
def _pack_fixer_owners() -> dict[str, str]:
    """Map fixer name -> owning installed pack (from ``provides.fixers``)."""
    out: dict[str, str] = {}
    for info in active_packs():
        if info.builtin or info.manifest is None or not info.may_ship_code:
            continue
        for name in info.manifest.fixers:
            out.setdefault(name, info.id)
    return out


@lru_cache(maxsize=1)
def _ownership() -> dict[tuple[str, str], str]:
    """Map ``(kind, name)`` -> owning pack id from built-in ``members``."""
    out: dict[tuple[str, str], str] = {}
    for info in active_packs():
        if not info.builtin or info.manifest is None:
            continue
        for kind, names in info.manifest.members.items():
            for name in names:
                out.setdefault((kind, name), info.id)
    return out


def clear_cache() -> None:
    """Drop every cached discovery result (tests, ``ovid2 tools pack`` after edits)."""
    all_packs.cache_clear()
    _ownership.cache_clear()
    _pack_fixer_owners.cache_clear()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.packs.discovery is a library module.")
