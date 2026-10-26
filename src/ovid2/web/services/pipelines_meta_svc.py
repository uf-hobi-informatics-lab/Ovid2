"""Pipeline discovery service — read-only.

Walks the ``ovid2_pipelines.*`` PEP-420 namespace on ``sys.path`` and
returns metadata about each pipeline:

- Declared ``mappers_type`` and other ``UPPER_CASE`` constants from
  ``pipeline_settings.py``.
- Filesystem roster of formatters (``formatters/*.yaml`` and any legacy
  ``formatters/*.py``) and mappers (``mappers/*.yaml`` / ``*.py``).
- Line-count and modification time of ``dictionaries.py`` /
  ``fixer_configs.py`` so the UI can offer a "large file" hint before
  fetching bytes.

Deliberately import-only against ``importlib`` / ``pkgutil`` — we cannot
touch ``ovid2.formatters`` or ``ovid2.mappers`` from the web tier (see
``tests/contract/test_web_no_pipeline_imports.py``).
"""

from __future__ import annotations

import importlib
import pkgutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ovid2.web.models import PipelineDetail, PipelineFileInfo, PipelineSummary


def _pipelines_pkg_paths() -> list[Path]:
    """Return every filesystem path that contributes to ``ovid2_pipelines``.

    PEP 420 namespace packages can span multiple directories on
    ``sys.path``. Each contributing directory becomes one entry.
    """
    try:
        pkg = importlib.import_module("ovid2_pipelines")
    except ModuleNotFoundError:
        return []
    return [Path(p) for p in getattr(pkg, "__path__", [])]


def discover_pipelines() -> list[str]:
    """Return every pipeline name found under ``ovid2_pipelines``.

    The listing is deduplicated and sorted so callers can rely on
    stable ordering.
    """
    try:
        pkg = importlib.import_module("ovid2_pipelines")
    except ModuleNotFoundError:
        return []
    names: set[str] = set()
    for module_info in pkgutil.iter_modules(pkg.__path__):
        if module_info.ispkg:
            names.add(module_info.name)
    return sorted(names)


def _pipeline_path(pipeline: str) -> Path | None:
    """Return the on-disk directory of a pipeline package, or None."""
    try:
        pkg = importlib.import_module(f"ovid2_pipelines.{pipeline}")
    except ModuleNotFoundError:
        return None
    for base in getattr(pkg, "__path__", []):
        candidate = Path(base)
        if candidate.is_dir():
            return candidate
    return None


def _load_pipeline_settings(pipeline: str) -> dict[str, Any]:
    """Return the pipeline's ``pipeline_settings`` module as a plain dict.

    Silently returns ``{}`` when the module is missing so the UI can
    still list a pipeline package that has yet to be populated.
    """
    try:
        mod = importlib.import_module(f"ovid2_pipelines.{pipeline}.pipeline_settings")
    except ModuleNotFoundError:
        return {}
    result: dict[str, Any] = {}
    for name in dir(mod):
        if name.startswith("_"):
            continue
        if name == "mappers_type" or name.isupper():
            value = getattr(mod, name)
            # Coerce non-JSON-safe values (Path, tuple, set) into strings.
            if isinstance(value, (str, int, float, bool)) or value is None:
                result[name] = value
            elif isinstance(value, (list, tuple, set, frozenset)):
                result[name] = [str(v) for v in value]
            elif isinstance(value, dict):
                result[name] = {str(k): str(v) for k, v in value.items()}
            else:
                result[name] = str(value)
    return result


def _file_info(path: Path) -> PipelineFileInfo | None:
    """Return :class:`PipelineFileInfo` for one file, or None if absent."""
    if not path.is_file():
        return None
    stat = path.stat()
    line_count = 0
    try:
        with path.open(encoding="utf-8") as fh:
            for _ in fh:
                line_count += 1
    except OSError:
        line_count = 0
    return PipelineFileInfo(
        path=str(path),
        name=path.name,
        size_bytes=int(stat.st_size),
        line_count=line_count,
        mtime=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    )


def _list_spec_files(root: Path) -> list[PipelineFileInfo]:
    """Return YAML/Python spec files under ``root`` as :class:`PipelineFileInfo`."""
    if not root.is_dir():
        return []
    infos: list[PipelineFileInfo] = []
    for entry in sorted(root.iterdir()):
        if entry.name.startswith("_"):
            continue
        if entry.suffix not in {".yaml", ".yml", ".py"}:
            continue
        info = _file_info(entry)
        if info is not None:
            infos.append(info)
    return infos


def list_pipelines() -> list[PipelineSummary]:
    """Return a summary row per discoverable pipeline."""
    summaries: list[PipelineSummary] = []
    for name in discover_pipelines():
        settings = _load_pipeline_settings(name)
        pkg_path = _pipeline_path(name)
        formatter_count = 0
        mapper_count = 0
        if pkg_path is not None:
            formatter_count = len(
                [
                    e
                    for e in (pkg_path / "formatters").glob("*")
                    if e.suffix in {".yaml", ".yml", ".py"} and not e.name.startswith("_")
                ]
            )
            mapper_count = len(
                [
                    e
                    for e in (pkg_path / "mappers").glob("*")
                    if e.suffix in {".yaml", ".yml", ".py"} and not e.name.startswith("_")
                ]
            )
        summaries.append(
            PipelineSummary(
                name=name,
                mappers_type=str(settings.get("mappers_type") or ""),
                default_db=str(settings.get("DEFAULT_PIPELINE_DB_NAME") or ""),
                path=str(pkg_path) if pkg_path else "",
                formatter_count=formatter_count,
                mapper_count=mapper_count,
            )
        )
    return summaries


def get_pipeline(name: str) -> PipelineDetail | None:
    """Return detailed metadata for one pipeline, or None if unknown."""
    if name not in set(discover_pipelines()):
        return None
    pkg_path = _pipeline_path(name)
    if pkg_path is None:
        return None
    settings = _load_pipeline_settings(name)
    return PipelineDetail(
        name=name,
        path=str(pkg_path),
        settings=settings,
        formatters=_list_spec_files(pkg_path / "formatters"),
        mappers=_list_spec_files(pkg_path / "mappers"),
        pipeline_settings_file=_file_info(pkg_path / "pipeline_settings.py"),
        dictionaries_file=_file_info(pkg_path / "dictionaries.py"),
        fixer_configs_file=_file_info(pkg_path / "fixer_configs.py"),
    )


# ---------------------------------------------------------------------------
# File preview
# ---------------------------------------------------------------------------

#: Maximum bytes returned by :func:`read_pipeline_file`. Large pipeline
#: dictionaries (``DEMO`` runs 3.6k lines) still fit comfortably.
MAX_PREVIEW_BYTES: int = 1_048_576  # 1 MiB


def _iter_allowed_files(pkg_path: Path) -> list[Path]:
    """Return the whitelist of files a caller may fetch for one pipeline."""
    allowed: list[Path] = []
    for rel in ("pipeline_settings.py", "dictionaries.py", "fixer_configs.py"):
        candidate = pkg_path / rel
        if candidate.is_file():
            allowed.append(candidate)
    for sub in ("formatters", "mappers"):
        sub_dir = pkg_path / sub
        if not sub_dir.is_dir():
            continue
        for entry in sub_dir.iterdir():
            if entry.suffix in {".yaml", ".yml", ".py"} and not entry.name.startswith("_"):
                allowed.append(entry)
    return allowed


def read_pipeline_file(pipeline: str, relative_path: str) -> tuple[Path, str] | None:
    """Return the resolved path and file contents, with anti-traversal checks.

    Args:
        pipeline: Pipeline name (must exist under ``ovid2_pipelines``).
        relative_path: Path relative to the pipeline package
            (``pipeline_settings.py``, ``formatters/demographic.yaml``, …).

    Returns:
        ``(absolute_path, contents)`` on success, or ``None`` when the
        target is missing or falls outside the pipeline package.
    """
    pkg_path = _pipeline_path(pipeline)
    if pkg_path is None:
        return None
    candidate = (pkg_path / relative_path).resolve()
    try:
        candidate.relative_to(pkg_path.resolve())
    except ValueError:
        return None
    if candidate not in {p.resolve() for p in _iter_allowed_files(pkg_path)}:
        return None
    if not candidate.is_file():
        return None
    size = candidate.stat().st_size
    if size > MAX_PREVIEW_BYTES:
        # Truncate — mostly for ``dictionaries.py`` bombs.
        with candidate.open(encoding="utf-8", errors="replace") as fh:
            content = fh.read(MAX_PREVIEW_BYTES)
        content += f"\n\n# --- truncated at {MAX_PREVIEW_BYTES} bytes (file is {size} bytes) ---\n"
        return candidate, content
    return candidate, candidate.read_text(encoding="utf-8", errors="replace")
