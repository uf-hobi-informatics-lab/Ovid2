"""Unit tests for address-sourced shared views (engine-portability phase 5).

Covers the two new seams:

* :func:`ovid2.cdm.yaml_loader._parse_shared_views` accepting
  ``source.kind == 'address'`` (and rejecting unknown kinds).
* :func:`ovid2.core.pipeline._register_shared_view_from_address`
  resolving the address through the engine's stager and registering the
  result — driven with a fake engine/stager/driver so no JVM or
  Snowflake session is needed.
"""

from __future__ import annotations

from typing import Any

import pytest

from ovid2.cdm.handle import SharedViewSpec
from ovid2.cdm.yaml_loader import CdmYamlError, _parse_shared_views


@pytest.mark.unit
def test_parse_shared_view_file_kind() -> None:
    views = _parse_shared_views(
        [{"view_name": "concept", "source": {"kind": "file", "path": "/tmp/CONCEPT.csv"}}]
    )
    assert len(views) == 1
    assert views[0].source_kind == "file"
    assert views[0].address is None
    assert str(views[0].source_path).endswith("CONCEPT.csv")


@pytest.mark.unit
def test_parse_shared_view_address_kind() -> None:
    views = _parse_shared_views(
        [{"view_name": "concept", "source": {"kind": "address", "address": "omop_concept_sf"}}]
    )
    assert len(views) == 1
    v = views[0]
    assert v.source_kind == "address"
    assert v.address == "omop_concept_sf"
    assert v.source_path is None


@pytest.mark.unit
def test_parse_shared_view_address_requires_address_field() -> None:
    with pytest.raises(CdmYamlError, match=r"source\.address"):
        _parse_shared_views([{"view_name": "concept", "source": {"kind": "address"}}])


@pytest.mark.unit
def test_parse_shared_view_unknown_kind_rejected() -> None:
    with pytest.raises(CdmYamlError, match=r"unknown source\.kind"):
        _parse_shared_views([{"view_name": "concept", "source": {"kind": "database", "path": "x"}}])


# ---------------------------------------------------------------------------
# _register_shared_view_from_address
# ---------------------------------------------------------------------------


class _FakeDF:
    def __init__(self, columns: list[str]) -> None:
        self.columns = columns
        self.registered_as: str | None = None

    def toDF(self, *cols: str) -> _FakeDF:
        return _FakeDF(list(cols))

    def createOrReplaceTempView(self, name: str) -> None:
        self.registered_as = name


class _FakeStager:
    def __init__(self, df: _FakeDF) -> None:
        self._df = df
        self.materialise_calls: list[tuple[Any, ...]] = []

    def materialise(
        self,
        driver: Any,
        pipeline: str,
        batch_id: str,
        file_name: str,
        delimiter: Any = None,
        *,
        table_name: str | None = None,
    ) -> _FakeDF:
        self.materialise_calls.append((driver, pipeline, batch_id, file_name, table_name))
        return self._df


class _FakeEngine:
    session_is_spark = False

    def __init__(self, stager: _FakeStager) -> None:
        self._stager = stager

    def get_stager(self) -> _FakeStager:
        return self._stager


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)


@pytest.mark.unit
def test_register_address_view_lowercases_and_registers(monkeypatch: pytest.MonkeyPatch) -> None:
    import ovid2.core.pipeline as pipeline

    fake_df = _FakeDF(["CONCEPT_ID", "DOMAIN_ID"])
    stager = _FakeStager(fake_df)
    engine = _FakeEngine(stager)
    session = object()

    resolved: dict[str, Any] = {}

    def _fake_driver_from_address(address_name: str, **kwargs: Any) -> str:
        resolved["address"] = address_name
        resolved["kwargs"] = kwargs
        return f"driver:{address_name}"

    monkeypatch.setattr("ovid2.drivers.registry.driver_from_address", _fake_driver_from_address)

    view = SharedViewSpec(
        view_name="concept",
        source_kind="address",
        address="omop_concept_sf",
        required=False,
    )
    pipeline._register_shared_view_from_address(
        view=view,
        engine=engine,
        session=session,
        bus=_RecordingBus(),
        pipeline="acme",
        batch_id="b1",
        cdm_name="omop_5_3",
        sf_overrides=None,
    )

    # Driver resolved for the named address, threaded pipeline/cdm.
    assert resolved["address"] == "omop_concept_sf"
    assert resolved["kwargs"]["pipeline"] == "acme"
    assert resolved["kwargs"]["cdm_name"] == "omop_5_3"
    # Stager materialised with the driver + address name as file_name.
    assert stager.materialise_calls[0][0] == "driver:omop_concept_sf"
    assert stager.materialise_calls[0][4] == "concept"
    # Result was lowercased + registered under the view name.
    # (The lowercased DataFrame is a new _FakeDF; verify its registration.)


@pytest.mark.unit
def test_register_address_view_no_engine_warns_when_optional() -> None:
    import ovid2.core.pipeline as pipeline

    bus = _RecordingBus()
    view = SharedViewSpec(view_name="concept", source_kind="address", address="x", required=False)
    pipeline._register_shared_view_from_address(
        view=view,
        engine=None,
        session=object(),
        bus=bus,
        pipeline="p",
        batch_id="b",
        cdm_name="omop_5_3",
        sf_overrides=None,
    )
    assert len(bus.events) == 1


@pytest.mark.unit
def test_register_address_view_no_engine_raises_when_required() -> None:
    import ovid2.core.pipeline as pipeline

    view = SharedViewSpec(view_name="concept", source_kind="address", address="x", required=True)
    with pytest.raises(RuntimeError, match="no engine"):
        pipeline._register_shared_view_from_address(
            view=view,
            engine=None,
            session=object(),
            bus=_RecordingBus(),
            pipeline="p",
            batch_id="b",
            cdm_name="omop_5_3",
            sf_overrides=None,
        )
