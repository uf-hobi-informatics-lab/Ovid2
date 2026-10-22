"""Unit tests for ``ovid2.web.services.addresses_svc``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ovid2.web.services import addresses_svc


@pytest.mark.unit
def test_list_returns_disc_config(sample_address: Path) -> None:
    """A disc config on the search path shows up in the listing."""
    result = addresses_svc.list_addresses()
    names = {a.name for a in result}
    assert "test_disc" in names


@pytest.mark.unit
def test_get_masks_password(isolated_xdg: Path) -> None:
    """Snowflake password values are replaced with ``***``."""
    from ovid2.config import paths as xdg_paths

    configs = xdg_paths.configs_dir()
    configs.mkdir(parents=True, exist_ok=True)
    (configs / "sf_test.json").write_text(
        json.dumps(
            {
                "src_name": "snowflake",
                "src_args": {
                    "account": "acct1",
                    "user": "u1",
                    "password": "hunter2",
                    "warehouse": "wh",
                    "database": "db",
                    "schema": "s",
                },
            }
        ),
        encoding="utf-8",
    )
    detail = addresses_svc.get_address("sf_test")
    assert detail is not None
    assert detail.src_name == "snowflake"
    assert detail.src_args["password"] == "***"
    assert detail.src_args["account"] == "acct1"


@pytest.mark.unit
def test_get_returns_none_for_missing(isolated_xdg: Path) -> None:
    """Missing configs surface as ``None``."""
    assert addresses_svc.get_address("does_not_exist") is None


@pytest.mark.unit
def test_masking_leaves_empty_values_alone(isolated_xdg: Path) -> None:
    """Empty password strings are not replaced (nothing to hide)."""
    from ovid2.config import paths as xdg_paths

    configs = xdg_paths.configs_dir()
    configs.mkdir(parents=True, exist_ok=True)
    (configs / "sf_blank.json").write_text(
        json.dumps({"src_name": "snowflake", "src_args": {"user": "u", "password": ""}}),
        encoding="utf-8",
    )
    detail = addresses_svc.get_address("sf_blank")
    assert detail is not None
    assert detail.src_args["password"] == ""
