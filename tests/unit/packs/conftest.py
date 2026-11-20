"""Shared fixtures for domain-pack unit tests.

Every test gets its own ``$XDG_CONFIG_HOME``, an empty folder-pack root
(``$OVID2_PACKS_DIR``) and no ``OVID2_PACKS_ENABLED``. Discovery caches
are cleared before and after, so the host's real packs never leak in.

``pip_pack`` builds a real installed distribution on ``sys.path`` (an
import package plus a ``.dist-info`` with an ``ovid2.packs`` entry
point), so the installed-pack path runs through the real
:func:`importlib.metadata.entry_points`.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml


def _clear() -> None:
    from ovid2.packs import clear_cache

    clear_cache()


@pytest.fixture()
def pack_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolate XDG config + the folder-pack root; yield the folder-pack root."""
    config = tmp_path / "xdg_config"
    config.mkdir()
    packs_root = tmp_path / "packs"
    packs_root.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("OVID2_PACKS_DIR", str(packs_root))
    monkeypatch.delenv("OVID2_PACKS_ENABLED", raising=False)
    _clear()
    yield packs_root
    _clear()


def write_yaml(path: Path, payload: Any) -> Path:
    """Write ``payload`` as YAML at ``path`` (creating parents)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def make_pack(
    root: Path,
    pack_id: str,
    *,
    provides: dict[str, Any] | None = None,
    checks: list[dict[str, Any]] | None = None,
    packages: list[dict[str, Any]] | None = None,
    **manifest: Any,
) -> Path:
    """Write a minimal pack at ``root/pack_id`` and return its directory.

    Args:
        root: Parent directory (folder-pack root or an import package parent).
        pack_id: Pack id (also the directory name).
        provides: ``provides`` override (default: one CDM + checks + packages).
        checks: Health-check docs written under ``checks/``.
        packages: Package docs written under ``packages/``.
        **manifest: Extra top-level manifest keys.
    """
    pack_dir = root / pack_id
    if provides is None:
        provides = {"cdms": [pack_id], "checks_dir": "checks", "packages_dir": "packages"}
    write_yaml(
        pack_dir / "pack.yaml",
        {"v": 1, "id": pack_id, "version": "0.1.0", "label": pack_id, "provides": provides}
        | manifest,
    )
    if pack_id in (provides.get("cdms") or []):
        write_yaml(
            pack_dir / "cdms" / pack_id / "cdm.yaml",
            {
                "v": 1,
                "name": pack_id,
                "version": "0.1",
                "tables": ["subject"],
                "reference_tables": [],
                "flavour_of_null": ["", None],
                "id_columns": {"patient": {"column": "SUBJECT_ID", "width": 64}},
            },
        )
        write_yaml(
            pack_dir / "cdms" / pack_id / "tables" / "subject.yaml",
            {
                "v": 1,
                "name": "subject",
                "composite_key": ["SUBJECT_ID"],
                "fields": [
                    {"name": "SUBJECT_ID", "rdbms_type": "RDBMS Text(64)", "order": 1},
                    {"name": "SEX", "rdbms_type": "RDBMS Text(2)", "order": 2},
                ],
            },
        )
    if checks is None:
        checks = [
            {
                "id": f"{pack_id}.subject_present",
                "name": "SUBJECT_PRESENT",
                "part": "part1",
                "description": "SUBJECT is present",
                "operator": "table_present",
                "source_tables": ["SUBJECT"],
                "severity": "blocker",
                "applies_to_cdms": [pack_id],
                "remediation": {"hint": "Deliver SUBJECT."},
            }
        ]
    for doc in checks:
        write_yaml(pack_dir / "checks" / f"{doc['id'].split('.')[-1]}.yaml", doc)
    if packages is None:
        packages = [
            {
                "id": f"{pack_id}.baseline",
                "label": "Baseline",
                "released": "2026-01-01",
                "checks": [c["id"] for c in checks],
            }
        ]
    for doc in packages:
        write_yaml(pack_dir / "packages" / f"{doc['id'].split('.')[-1]}.yaml", doc)
    return pack_dir


@pytest.fixture()
def pip_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pack_env: Path
) -> Iterator[Callable[..., Path]]:
    """Return a factory that installs a pack as a real distribution on ``sys.path``.

    ``pip_pack(pack_id, files={rel: text}, **make_pack_kwargs)`` writes
    ``site/ovid2_pack_<id>/`` (with ``__init__.py`` + ``pack.yaml`` +
    content + extra ``files``) and ``site/ovid2_pack_<id>-0.1.0.dist-info``
    whose ``entry_points.txt`` registers ``<id> = ovid2_pack_<id>``.
    """
    site = tmp_path / "site"
    site.mkdir()
    monkeypatch.syspath_prepend(str(site))
    created: list[str] = []

    def _factory(
        pack_id: str,
        *,
        files: dict[str, str] | None = None,
        entry_name: str | None = None,
        **kwargs: Any,
    ) -> Path:
        module = f"ovid2_pack_{pack_id}"
        built = make_pack(site, pack_id, **kwargs)
        pkg = site / module
        built.rename(pkg)
        (pkg / "__init__.py").write_text(f'"""Pack {pack_id}."""\n', encoding="utf-8")
        for rel, text in (files or {}).items():
            (pkg / rel).write_text(text, encoding="utf-8")
        dist = site / f"{module}-0.1.0.dist-info"
        dist.mkdir()
        (dist / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {module.replace('_', '-')}\nVersion: 0.1.0\n",
            encoding="utf-8",
        )
        (dist / "entry_points.txt").write_text(
            f"[ovid2.packs]\n{entry_name or pack_id} = {module}\n", encoding="utf-8"
        )
        created.append(module)
        _clear()
        return pkg

    yield _factory
    for module in created:
        for name in [m for m in sys.modules if m == module or m.startswith(module + ".")]:
            del sys.modules[name]
