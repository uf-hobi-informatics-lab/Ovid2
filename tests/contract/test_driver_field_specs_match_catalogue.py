"""Contract: native driver field specs agree *structurally* with the web catalogue.

The web forms render from ``ovid2.connections.registry``
(``connection_field_spec`` / ``location_field_spec``), while
``Driver.connection_fields()`` / ``location_fields()`` on native drivers
come from ``drivers/<type>/metadata.py``. Both are hand-maintained;
``test_connection_catalogues_synchronised.py`` only compares key sets.

This test pins everything that changes behaviour — field names, order,
``required``, ``default``, widget kind, and which fields are secrets —
for every type in :data:`ovid2.drivers.registry.DRIVERS`. Help text is
deliberately *not* compared (the native copies carry richer prose).
"""

from __future__ import annotations

from typing import Any

import pytest

from ovid2.connections.registry import connection_field_spec, location_field_spec
from ovid2.drivers.registry import DRIVERS, load_class


def _catalogue_widget(entry: dict[str, Any], *, location: bool) -> str:
    if entry.get("multiline"):
        return "multiline"
    if location:
        return str(entry.get("type") or "str")
    return str(entry.get("widget") or "str")


def _norm_catalogue(entries: list[dict[str, Any]], *, location: bool) -> list[tuple[Any, ...]]:
    return [
        (
            e["name"],
            bool(e.get("required", False)),
            e.get("default"),
            _catalogue_widget(e, location=location),
        )
        for e in entries
    ]


def _norm_driver(entries: Any) -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = []
    for f in entries:
        if isinstance(f, dict):
            out.append(
                (
                    f["name"],
                    bool(f.get("required", False)),
                    f.get("default"),
                    str(f.get("widget") or f.get("type") or "str"),
                )
            )
        else:
            out.append((f.name, bool(f.required), f.default, str(f.widget or "str")))
    return out


@pytest.mark.contract
@pytest.mark.parametrize("type_name", sorted(DRIVERS))
def test_connection_fields_match_catalogue(type_name: str) -> None:
    """``args`` and ``secrets`` agree on name / order / required / default / widget."""
    spec = load_class(type_name).connection_fields()
    cat = connection_field_spec(type_name)
    assert _norm_driver(spec.args) == _norm_catalogue(cat["args"], location=False), (
        f"{type_name}: connection args drifted between drivers/{type_name}/metadata.py "
        "and ovid2.connections.registry._FIELD_SPECS"
    )
    assert _norm_driver(spec.secrets) == _norm_catalogue(cat["secrets"], location=False), (
        f"{type_name}: connection secrets drifted between the driver and the catalogue"
    )
    assert (spec.note is None) == (cat["note"] is None), f"{type_name}: note presence differs"


@pytest.mark.contract
@pytest.mark.parametrize("type_name", sorted(DRIVERS))
def test_location_fields_match_catalogue(type_name: str) -> None:
    """Location fields agree on name / order / required / default / widget."""
    fields = load_class(type_name).location_fields()
    cat = location_field_spec(type_name)
    assert _norm_driver(fields) == _norm_catalogue(cat, location=True), (
        f"{type_name}: location fields drifted between the driver and "
        "ovid2.connections.registry._LOCATION_SPECS"
    )
