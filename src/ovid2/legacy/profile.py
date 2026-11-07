"""Per-pipeline ETL profile.

A pipeline selects its ETL profile with ``ETL_PROFILE`` in
``pipeline_settings.py``:

* ``"default"`` (implicit) — the Ovid2-native semantics: single lazy pass,
  composite-key dedup with null-key passthrough, per-pipeline mapper specs.
* ``"legacy_ovid"`` — byte-parity with the legacy ``<legacy-ovid>``
  ETL. Turns on, together:

  - **stage-boundary CSV round-trip emulation** — every legacy stage
    re-read the previous stage's tab CSV, which trimmed every value and
    turned empty strings into nulls (:mod:`ovid2.legacy.csv_roundtrip`);
  - **legacy deduplication** — first-column (or table-specific
    positional) keys, nulls compared equal, optional whole-row dedup
    (:mod:`ovid2.legacy.dedup`);
  - **CDM-default mapper specs** — the shared legacy mapper family
    (``mapping_scripts/<family>_mappers``) ported to YAML under
    ``resources/cdms/<cdm>/mappers/`` and used when the pipeline ships no
    spec of its own;
  - **legacy fixer reference resolution** — ``src1: mapped_<t>.csv``
    resolves to the *pre-fix* (mapped) table, exactly like the legacy
    fix chain that read reference files from ``mapper_output/``.

See ``docs/parity.md`` and
the adr-legacy-parity-port design note.
"""

from __future__ import annotations

import importlib
from functools import lru_cache
from typing import Any

DEFAULT_PROFILE: str = "default"
LEGACY_OVID_PROFILE: str = "legacy_ovid"
KNOWN_PROFILES: frozenset[str] = frozenset({DEFAULT_PROFILE, LEGACY_OVID_PROFILE})


@lru_cache(maxsize=256)
def _settings(pipeline: str) -> Any:
    """Return the pipeline's ``pipeline_settings`` module, or ``None``."""
    try:
        return importlib.import_module(f"ovid2_pipelines.{pipeline}.pipeline_settings")
    except ModuleNotFoundError:
        return None


def etl_profile(pipeline: str | None) -> str:
    """Return the ETL profile declared by ``pipeline``.

    Args:
        pipeline: Pipeline name (``None`` → default profile).

    Returns:
        One of :data:`KNOWN_PROFILES`.

    Raises:
        ValueError: If the pipeline declares an unknown profile.
    """
    if not pipeline:
        return DEFAULT_PROFILE
    settings = _settings(pipeline)
    raw = getattr(settings, "ETL_PROFILE", DEFAULT_PROFILE) if settings is not None else None
    profile = str(raw or DEFAULT_PROFILE)
    if profile not in KNOWN_PROFILES:
        raise ValueError(
            f"Pipeline '{pipeline}' declares unknown ETL_PROFILE '{profile}'; "
            f"expected one of {sorted(KNOWN_PROFILES)}."
        )
    return profile


def is_legacy_ovid(pipeline: str | None) -> bool:
    """Return ``True`` when ``pipeline`` runs under the ``legacy_ovid`` profile."""
    return etl_profile(pipeline) == LEGACY_OVID_PROFILE


def pipeline_setting(pipeline: str | None, name: str, default: Any = None) -> Any:
    """Return ``pipeline_settings.<name>`` for ``pipeline`` (or ``default``)."""
    if not pipeline:
        return default
    settings = _settings(pipeline)
    return getattr(settings, name, default) if settings is not None else default
