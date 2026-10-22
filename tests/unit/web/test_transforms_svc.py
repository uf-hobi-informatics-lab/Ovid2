"""Unit tests for the web-tier transforms catalogue service.

These run without Spark — the service only reads the web-safe catalogue
mirror and the JSON schema.
"""

from __future__ import annotations

import pytest

from ovid2.web.services import transforms_svc


@pytest.mark.unit
def test_list_transforms_returns_full_catalogue() -> None:
    """The catalogue lists every transform with discovery metadata."""
    entries = transforms_svc.list_transforms()
    assert entries, "Catalogue should not be empty."
    for entry in entries:
        assert set(entry) >= {
            "name",
            "summary",
            "category",
            "requires_family",
            "params_schema",
        }


@pytest.mark.unit
def test_list_transforms_category_filter() -> None:
    """Filtering by category returns only that category."""
    sampling = transforms_svc.list_transforms("sampling")
    assert sampling
    assert all(e["category"] == "sampling" for e in sampling)
    assert transforms_svc.list_transforms("does_not_exist") == []


@pytest.mark.unit
def test_validate_good_spec() -> None:
    """A well-formed spec with a known op validates cleanly."""
    result = transforms_svc.validate_transform_spec(
        {"v": 1, "steps": [{"op": "to_upper", "columns": ["SEX"]}]}
    )
    assert result == {"ok": True, "errors": []}


@pytest.mark.unit
def test_validate_unknown_op() -> None:
    """An unknown op name is reported as an error."""
    result = transforms_svc.validate_transform_spec(
        {"v": 1, "steps": [{"op": "totally_made_up"}]}
    )
    assert result["ok"] is False
    assert any("totally_made_up" in e for e in result["errors"])


@pytest.mark.unit
def test_validate_missing_steps() -> None:
    """A spec missing `steps` fails schema validation."""
    result = transforms_svc.validate_transform_spec({"v": 1})
    assert result["ok"] is False
    assert result["errors"]
