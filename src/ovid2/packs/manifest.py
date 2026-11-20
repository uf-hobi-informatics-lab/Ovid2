"""Domain-pack manifest model (``pack.yaml``, schema ``pack.v1.json``).

Pure data + validation. Imports only :mod:`yaml`, :mod:`jsonschema` and
the standard library so every tier (web, MCP-facing REST, executor) can
read manifests without pulling pipeline code — see
``tests/contract/test_packs_tier_neutral.py``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "MANIFEST_FILE",
    "PackManifest",
    "PackManifestError",
    "load_manifest",
    "parse_manifest",
    "version_satisfies",
]

#: File name of a pack manifest inside the pack root.
MANIFEST_FILE = "pack.yaml"

#: Keys of ``provides`` that name a content directory relative to the root.
DIR_KEYS: tuple[str, ...] = (
    "checks_dir",
    "packages_dir",
    "vocabularies_dir",
    "bindings_dir",
    "report_templates_dir",
)


class PackManifestError(ValueError):
    """Raised when a ``pack.yaml`` is missing, unreadable or invalid."""


@dataclass(frozen=True)
class PackManifest:
    """A parsed, schema-valid ``pack.yaml``.

    Attributes:
        id: Stable pack id (also the namespace prefix of its content).
        version: Pack version string.
        label: Human label.
        description: One-paragraph description.
        domain: Domain slug (``healthcare`` …), may be blank.
        homepage: Project URL, may be blank.
        requires_ovid2: Version constraint string, may be blank.
        provides: The raw ``provides`` mapping.
    """

    id: str
    version: str
    label: str
    description: str = ""
    domain: str = ""
    homepage: str = ""
    requires_ovid2: str = ""
    provides: dict[str, Any] = field(default_factory=dict)

    @property
    def cdms(self) -> tuple[str, ...]:
        """CDM names the pack provides."""
        return tuple(str(c) for c in self.provides.get("cdms") or ())

    @property
    def parts(self) -> tuple[tuple[str, str], ...]:
        """Extra ``(part_id, label)`` buckets declared by the pack."""
        return tuple((str(p["id"]), str(p["label"])) for p in self.provides.get("parts") or ())

    @property
    def fixers(self) -> dict[str, dict[str, Any]]:
        """Web-safe metadata of the fixers the pack ships (name -> meta)."""
        raw = self.provides.get("fixers") or {}
        return {str(k): dict(v) for k, v in raw.items()}

    @property
    def members(self) -> dict[str, tuple[str, ...]]:
        """Built-in packs: core content the pack owns, by kind."""
        raw = self.provides.get("members") or {}
        return {str(k): tuple(str(x) for x in v) for k, v in raw.items()}

    def dir(self, key: str) -> str | None:
        """Return the relative content directory for ``key`` (or ``None``)."""
        value = self.provides.get(key)
        return str(value) if value else None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of the manifest."""
        return {
            "id": self.id,
            "version": self.version,
            "label": self.label,
            "description": self.description,
            "domain": self.domain,
            "homepage": self.homepage,
            "requires_ovid2": self.requires_ovid2,
            "provides": json.loads(json.dumps(self.provides)),
        }


def _schema() -> dict[str, Any]:
    ref = resources.files("ovid2.resources.schemas") / "pack.v1.json"
    with ref.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):  # pragma: no cover - bundled file
        raise PackManifestError("pack.v1.json is not an object")
    return data


def parse_manifest(raw: Any, *, source: str = "pack.yaml") -> PackManifest:
    """Validate ``raw`` against ``pack.v1.json`` and build a manifest.

    Args:
        raw: The parsed YAML document.
        source: Label used in error messages.

    Raises:
        PackManifestError: When the document does not match the schema.
    """
    import jsonschema

    if not isinstance(raw, dict):
        raise PackManifestError(f"{source}: top-level YAML must be a mapping")
    try:
        jsonschema.validate(raw, _schema())
    except jsonschema.ValidationError as exc:
        where = "/".join(str(p) for p in exc.absolute_path) or "(root)"
        raise PackManifestError(f"{source}: {where}: {exc.message}") from exc
    return PackManifest(
        id=str(raw["id"]),
        version=str(raw["version"]),
        label=str(raw["label"]),
        description=str(raw.get("description", "")),
        domain=str(raw.get("domain", "")),
        homepage=str(raw.get("homepage", "")),
        requires_ovid2=str(raw.get("requires_ovid2", "")),
        provides=dict(raw.get("provides") or {}),
    )


def load_manifest(root: Path) -> PackManifest:
    """Read and validate ``<root>/pack.yaml``.

    Raises:
        PackManifestError: When the file is missing, unreadable or invalid.
    """
    path = root / MANIFEST_FILE
    if not path.is_file():
        raise PackManifestError(f"{root}: no {MANIFEST_FILE}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise PackManifestError(f"{path}: {exc}") from exc
    return parse_manifest(raw, source=str(path))


_CONSTRAINT = re.compile(r"^\s*(>=|<=|==|!=|>|<)\s*([0-9][0-9A-Za-z.\-+]*)\s*$")


def _version_key(version: str) -> tuple[int, ...]:
    """Return the leading numeric release tuple of ``version`` (``0.1.0`` → ``(0, 1, 0)``)."""
    parts: list[int] = []
    for chunk in version.split("."):
        digits = re.match(r"\d+", chunk)
        if digits is None:
            break
        parts.append(int(digits.group(0)))
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def version_satisfies(version: str, constraint: str) -> bool:
    """Return whether ``version`` meets every comma-separated ``constraint``.

    Supports ``>=``, ``<=``, ``>``, ``<``, ``==`` and ``!=`` on the
    numeric release segment (pre-release tags are ignored). An empty
    constraint is always satisfied.

    Raises:
        PackManifestError: When a constraint cannot be parsed.
    """
    if not constraint.strip():
        return True
    have = _version_key(version)
    for clause in constraint.split(","):
        if not clause.strip():
            continue
        match = _CONSTRAINT.match(clause)
        if match is None:
            raise PackManifestError(f"requires_ovid2: cannot parse constraint {clause.strip()!r}")
        op, want_raw = match.groups()
        want = _version_key(want_raw)
        ok = {
            ">=": have >= want,
            "<=": have <= want,
            ">": have > want,
            "<": have < want,
            "==": have == want,
            "!=": have != want,
        }[op]
        if not ok:
            return False
    return True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.packs.manifest is a library module.")
