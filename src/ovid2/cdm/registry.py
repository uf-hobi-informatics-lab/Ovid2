"""CDM registry — one central catalogue, keyed by CDM name.

Any CDM that Ovid2 knows how to run must call :func:`register` at import
time. The pipeline reads the active CDM via :func:`get_cdm`, keyed by
``pipeline_settings.cdm``. There is no other way in — the ETL engine
never imports a CDM module directly.

Currently registered (see ``ovid2/cdm/__init__.py`` for the wiring):

- ``pcornet_7_0`` — PCORnet CDM v7.0 (default).
- ``ovid2_pipeline_veritas_v1`` — Veritas-flavoured pipeline CDM.
- ``ovid2_pipeline_nursing_v1`` — Nursing-flavoured pipeline CDM.
- ``ovid2_pipeline_tumor_v1`` — Tumor Registry-flavoured pipeline CDM.
"""

from __future__ import annotations

from typing import Final

from ovid2.cdm.handle import CdmHandle

_REGISTRY: Final[dict[str, CdmHandle]] = {}
DEFAULT_CDM: Final[str] = "pcornet_7_0"

#: Well-known *package-free* pipeline labels used by the data-health /
#: EDC-checks surfaces to run against **any** database of a given CDM
#: without a per-partner ``ovid2_pipelines.<name>`` package. The label is
#: only a report/grouping tag: the database and schema come entirely from
#: the run-time-selected address (browsed storage account → database →
#: schema), and the CDM is pinned here rather than read from a package.
#: This is what lets one reusable EDC configuration cover every PCORnet
#: database instead of a duplicate pipeline per partner. See
#: ``.opencode/knowledge/delta`` and ``docs/`` for the rationale.
GENERIC_CDM_LABELS: Final[dict[str, str]] = {
    "pcornet_edc": "pcornet_7_0",
}


def register(handle: CdmHandle, *, overwrite: bool = False) -> None:
    """Register a CDM handle under its ``name``.

    Args:
        handle: The :class:`CdmHandle` to register.
        overwrite: When ``True``, replaces any existing entry with the
            same name. Default ``False`` raises on collision.

    Raises:
        ValueError: If ``handle.name`` is already registered and
            ``overwrite`` is ``False``.
    """
    if handle.name in _REGISTRY and not overwrite:
        raise ValueError(f"CDM '{handle.name}' is already registered.")
    _REGISTRY[handle.name] = handle


def get_cdm(name: str | None = None) -> CdmHandle:
    """Return the :class:`CdmHandle` for ``name`` (defaults to PCORnet 7.0).

    Args:
        name: CDM identifier, e.g. ``'pcornet_7_0'``. When ``None`` or
            empty, resolves to :data:`DEFAULT_CDM`.

    Raises:
        ValueError: If ``name`` is not registered.
    """
    key = name or DEFAULT_CDM
    if key not in _REGISTRY:
        raise ValueError(
            f"Unknown CDM '{key}'. Registered: {sorted(_REGISTRY.keys()) or '(none)'}."
        )
    return _REGISTRY[key]


def registered_cdms() -> list[str]:
    """Return the sorted list of registered CDM names."""
    return sorted(_REGISTRY.keys())


def resolve_cdm_for_pipeline(pipeline: str) -> CdmHandle:
    """Return the CDM handle configured for ``pipeline``.

    Resolution order:

    1. :data:`GENERIC_CDM_LABELS` — well-known package-free EDC labels
       (e.g. ``pcornet_edc``) pin the CDM directly, so a reusable
       data-health run can target any database of that CDM with no
       per-partner package.
    2. ``pipeline_settings.cdm`` — explicit CDM name (recommended).
    3. Legacy ``pipeline_settings.mappers_type`` mapping:

       - ``pcornet_mappers`` → ``pcornet_7_0``
       - ``veritas_mappers`` → ``ovid2_pipeline_veritas_v1``
       - ``nursing_mappers`` → ``ovid2_pipeline_nursing_v1``
       - ``tumor_mappers``   → ``ovid2_pipeline_tumor_v1``

    4. Default → :data:`DEFAULT_CDM` (``pcornet_7_0``).

    Args:
        pipeline: Pipeline name (e.g. ``'demo'``).

    Returns:
        The resolved :class:`CdmHandle`.

    Raises:
        ValueError: If the resolved CDM name is not registered.
    """
    import importlib

    # Generic package-free EDC labels win first and never touch the
    # filesystem — the label is intentionally decoupled from any
    # ``ovid2_pipelines.<name>`` package.
    if pipeline in GENERIC_CDM_LABELS:
        return get_cdm(GENERIC_CDM_LABELS[pipeline])

    legacy_mappers_to_cdm: dict[str, str] = {
        "pcornet_mappers": "pcornet_7_0",
        "veritas_mappers": "ovid2_pipeline_veritas_v1",
        "nursing_mappers": "ovid2_pipeline_nursing_v1",
        "tumor_mappers": "ovid2_pipeline_tumor_v1",
    }

    cdm_name: str | None = None
    try:
        settings = importlib.import_module(f"ovid2_pipelines.{pipeline}.pipeline_settings")
    except ModuleNotFoundError:
        settings = None

    if settings is not None:
        explicit = getattr(settings, "cdm", None)
        if explicit:
            cdm_name = str(explicit)
        else:
            legacy = getattr(settings, "mappers_type", None)
            if legacy:
                cdm_name = legacy_mappers_to_cdm.get(str(legacy))

    return get_cdm(cdm_name)


def _clear() -> None:
    """Drop every registration. Test-only."""
    _REGISTRY.clear()
