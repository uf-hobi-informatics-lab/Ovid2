"""Unit tests for :mod:`ovid2.web.services.cdms_svc`.

Covers the read-only surface: listing every registered CDM, fetching
full detail for a YAML-authored CDM (``pcornet_7_0``), and fetching
detail for a pipeline-family CDM declared in Python
(``ovid2_pipeline_nursing_v1``). We assert the sort order, the
composite-key propagation, and the ``has_yaml`` / ``python`` split.
"""

from __future__ import annotations

import pytest

from ovid2.web.services import cdms_svc


@pytest.mark.unit
def test_list_cdms_returns_all_registered() -> None:
    """Every CDM in ``ovid2.cdm.registry`` shows up in the summary."""
    rows = cdms_svc.list_cdms()
    names = {r.name for r in rows}
    # Bundled CDMs registered at package import time.
    assert "pcornet_7_0" in names
    assert "ovid2_pipeline_veritas_v1" in names
    assert "ovid2_pipeline_nursing_v1" in names
    assert "ovid2_pipeline_tumor_v1" in names


@pytest.mark.unit
def test_list_cdms_is_sorted() -> None:
    """The list is alphabetical by name for deterministic UI output."""
    rows = cdms_svc.list_cdms()
    names = [r.name for r in rows]
    assert names == sorted(names)


@pytest.mark.unit
def test_list_cdms_flags_yaml_authored() -> None:
    """PCORnet is YAML-authored; pipeline-family CDMs are Python-declared."""
    rows = {r.name: r for r in cdms_svc.list_cdms()}
    assert rows["pcornet_7_0"].has_yaml is True
    assert rows["ovid2_pipeline_veritas_v1"].has_yaml is False
    assert rows["ovid2_pipeline_nursing_v1"].has_yaml is False
    assert rows["ovid2_pipeline_tumor_v1"].has_yaml is False


@pytest.mark.unit
def test_list_cdms_table_count_matches_registry() -> None:
    """The reported table count matches ``composite_keys`` len."""
    from ovid2.cdm.registry import get_cdm

    rows = {r.name: r for r in cdms_svc.list_cdms()}
    for name, summary in rows.items():
        handle = get_cdm(name)
        assert summary.table_count == len(handle.composite_keys)


@pytest.mark.unit
def test_get_cdm_detail_unknown_returns_none() -> None:
    """Unknown CDM name resolves to ``None``, never raises."""
    assert cdms_svc.get_cdm_detail("no_such_cdm") is None


@pytest.mark.unit
def test_get_cdm_detail_pcornet_shape() -> None:
    """PCORnet 7.0 exposes tables, id_columns, and valuesets."""
    detail = cdms_svc.get_cdm_detail("pcornet_7_0")
    assert detail is not None
    assert detail.name == "pcornet_7_0"
    assert detail.version == "7.0"
    assert detail.has_yaml is True
    assert detail.yaml_dir and detail.yaml_dir.endswith("pcornet_7_0")

    # 27 tables, all present with composite keys.
    assert len(detail.tables) >= 20
    table_names = {t.name for t in detail.tables}
    assert "demographic" in table_names

    # demographic key = ("PATID",)
    demog = next(t for t in detail.tables if t.name == "demographic")
    assert demog.composite_key == ["PATID"]

    # id_columns include the patient role.
    roles = {c.role for c in detail.id_columns}
    assert "patient" in roles

    # Valuesets are present and sorted (table, column).
    assert detail.valuesets, "PCORnet has 129 bounded valuesets"
    pairs = [(v.table, v.column) for v in detail.valuesets]
    assert pairs == sorted(pairs)
    assert all(v.code_count > 0 for v in detail.valuesets)


@pytest.mark.unit
def test_get_cdm_detail_flavour_of_null_includes_sentinels() -> None:
    """PCORnet flavour-of-null contains NI/OT/UN plus empty + null."""
    detail = cdms_svc.get_cdm_detail("pcornet_7_0")
    assert detail is not None
    fon = detail.flavour_of_null
    # Non-None sentinels sort first.
    non_none = [v for v in fon if v is not None]
    assert non_none == sorted(non_none)
    assert {"NI", "OT", "UN"}.issubset(set(non_none))


@pytest.mark.unit
def test_get_cdm_detail_pipeline_family_no_yaml() -> None:
    """Pipeline-family CDMs report ``has_yaml=False`` and no yaml_dir."""
    detail = cdms_svc.get_cdm_detail("ovid2_pipeline_veritas_v1")
    assert detail is not None
    assert detail.has_yaml is False
    assert detail.yaml_dir is None


@pytest.mark.unit
def test_get_cdm_detail_reference_tables_are_sorted() -> None:
    """PCORnet reference tables are returned as a sorted list."""
    detail = cdms_svc.get_cdm_detail("pcornet_7_0")
    assert detail is not None
    assert detail.reference_tables == sorted(detail.reference_tables)
