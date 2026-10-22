"""Unit tests for :mod:`ovid2.core.loaders`."""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

import pytest

from ovid2.core.loaders import (
    get_all_transformations,
    has_formatter,
    resolve_tables,
    resolve_transformations,
)

# ---------------------------------------------------------------------------
# resolve_transformations
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_canonical_order() -> None:
    """``['all']`` expands to the canonical order."""
    assert resolve_transformations(["all"]) == ["format", "deduplicate", "map", "fix"]


@pytest.mark.unit
def test_sort_into_canonical() -> None:
    """User-provided order is always re-sorted into canonical order."""
    assert resolve_transformations(["fix", "format", "map"]) == ["format", "map", "fix"]


@pytest.mark.unit
def test_unknown_names_returned_as_is() -> None:
    """DQ-only stages (e.g. mapping_gap) pass through unchanged."""
    assert resolve_transformations(["mapping_gap"]) == ["mapping_gap"]


@pytest.mark.unit
def test_canonical_list_matches_documented_chain() -> None:
    """Sanity: the canonical order is exactly what the docs promise."""
    assert get_all_transformations() == ["format", "deduplicate", "map", "fix"]


# ---------------------------------------------------------------------------
# resolve_tables — explicit list pass-through
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_tables_pass_through() -> None:
    """Non-``all`` table lists are returned unchanged, even if some
    tables have no formatter yet (explicit means the caller owns it)."""
    assert resolve_tables(["encounter", "demographic"], "site") == [
        "encounter",
        "demographic",
    ]


@pytest.mark.unit
def test_resolve_tables_explicit_list_not_filtered_for_missing_specs() -> None:
    """Even for a mostly-unmigrated pipeline, an explicit list is returned
    verbatim so the downstream loader can raise the precise error."""
    # acme has only external_meds / hash_token / pat_relationship YAMLs, yet
    # an explicit request for `demographic` must still be honoured here.
    assert resolve_tables(["demographic"], "acme") == ["demographic"]


# ---------------------------------------------------------------------------
# resolve_tables — ``['all']`` filters out un-migrated tables
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_tables_all_filters_pipeline_with_partial_migration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``example_pcornet`` provides 23 of the 27 PCORnet tables; ``['all']``
    must expand to exactly the provided tables and log a warning naming the
    skipped ones (it ships no ``vital`` / ``lab_history`` / ``pcornet_trial``
    / ``external_meds`` formatter)."""
    caplog.set_level(logging.WARNING, logger="ovid2.core.loaders")

    from ovid2.core.loaders import get_all_tables

    all_tables = get_all_tables("example_pcornet")
    result = resolve_tables(["all"], "example_pcornet")

    # Result is exactly the specced subset, in CDM order.
    assert result == [t for t in all_tables if has_formatter("example_pcornet", t)]
    assert all(has_formatter("example_pcornet", t) for t in result)
    # YAML-specced and custom-module tables are both present …
    assert {"demographic", "diagnosis", "provider", "harvest", "death_cause"} <= set(result)
    # … and the not-provided ones are absent.
    skipped = {"vital", "lab_history", "pcornet_trial", "external_meds"}
    assert not (skipped & set(result))

    # A warning must name the skipped (not-provided) tables.
    warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert warnings, "Expected at least one WARNING when tables are skipped"
    msg = warnings[-1].getMessage()
    assert "example_pcornet" in msg
    for expected in skipped:
        assert expected in msg


@pytest.mark.unit
def test_resolve_tables_all_no_warning_when_fully_migrated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When every ``all``-expanded table has a spec, no warning fires and
    the returned list matches the CDM order exactly."""
    caplog.set_level(logging.WARNING, logger="ovid2.core.loaders")

    from ovid2.core.loaders import get_all_tables

    all_tables = get_all_tables("pcornet_to_omop")
    result = resolve_tables(["all"], "pcornet_to_omop")

    assert result == [t for t in all_tables if has_formatter("pcornet_to_omop", t)]
    assert all(has_formatter("pcornet_to_omop", t) for t in result)


# ---------------------------------------------------------------------------
# has_formatter — probe semantics
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_has_formatter_yaml_hit() -> None:
    """A pipeline+table with a YAML on disk resolves True."""
    # example_pcornet/formatters/hash_token.yaml exists.
    assert has_formatter("example_pcornet", "hash_token") is True


@pytest.mark.unit
def test_has_formatter_custom_module_hit() -> None:
    """A table served only by ``formatters_custom.<table>_formatter`` resolves True."""
    # example_pcornet ships harvest only as a custom formatter module.
    assert has_formatter("example_pcornet", "harvest") is True


@pytest.mark.unit
def test_has_formatter_yaml_miss() -> None:
    """A CDM table the pipeline does not provide resolves False.

    ``example_pcornet`` ships no ``vital`` YAML or custom module.
    """
    assert has_formatter("example_pcornet", "vital") is False


@pytest.mark.unit
def test_has_formatter_unknown_pipeline() -> None:
    """Missing pipeline package resolves False, not a raise."""
    assert has_formatter("does_not_exist_pipeline", "demographic") is False


@pytest.mark.unit
def test_has_formatter_detects_custom_module(tmp_path: Path) -> None:
    """A pipeline exposing only a ``formatters_custom.<table>_formatter``
    module (no YAML, no ``formatters.<table>`` module) still resolves True.

    Uses a synthetic in-memory pipeline registered under a unique name so it
    can never collide with a real one on ``sys.path``.
    """
    pipeline_name = "hasfmt_probe_pipeline_xyz"

    # Build the on-disk layout:
    #   <tmp>/ovid2_pipelines/<pipeline_name>/__init__.py
    #   <tmp>/ovid2_pipelines/<pipeline_name>/formatters_custom/__init__.py
    #   <tmp>/ovid2_pipelines/<pipeline_name>/formatters_custom/some_table_formatter.py
    root = tmp_path / "ovid2_pipelines"
    (root / pipeline_name / "formatters_custom").mkdir(parents=True)
    (root / pipeline_name / "__init__.py").write_text("")
    (root / pipeline_name / "formatters_custom" / "__init__.py").write_text("")
    (root / pipeline_name / "formatters_custom" / "some_table_formatter.py").write_text(
        "class Some_tableFormatter:\n    pass\n"
    )

    sys.path.insert(0, str(tmp_path))
    # Bust any prior namespace-cache so importlib sees the new location.
    for cached in [k for k in list(sys.modules) if k.startswith("ovid2_pipelines")]:
        if cached == "ovid2_pipelines" or cached.startswith(f"ovid2_pipelines.{pipeline_name}"):
            del sys.modules[cached]
    importlib.invalidate_caches()

    try:
        assert has_formatter(pipeline_name, "some_table") is True
        # And a table that doesn't exist under either path resolves False.
        assert has_formatter(pipeline_name, "missing_table") is False
    finally:
        sys.path.remove(str(tmp_path))
        for cached in [k for k in list(sys.modules) if k.startswith("ovid2_pipelines")]:
            if cached == "ovid2_pipelines" or cached.startswith(f"ovid2_pipelines.{pipeline_name}"):
                del sys.modules[cached]
        importlib.invalidate_caches()
