"""Snowflake tier resolution honours ``location.path`` before pipeline defaults.

Regression: ``config.addresses._resolve_snowflake_config`` applied the
pipeline's ``DEFAULT_PIPELINE_DB_NAME`` (tier 3) whenever the merged
args had no ``database`` key. A web-saved address stores its database in
``{"path": "SANDBOX"}``, so a copy labelled with a pipeline whose
default DB is ``CHAR_DEMO`` silently wrote into ``CHAR_DEMO`` instead
of ``SANDBOX``.
"""

from __future__ import annotations

import sys
import types

import pytest

from ovid2.config.addresses import _resolve_snowflake_config, split_snowflake_path


@pytest.fixture()
def demo_pipeline(monkeypatch: pytest.MonkeyPatch) -> str:
    """Install a fake ``ovid2_pipelines.demo_sf`` with a default DB."""
    settings = types.ModuleType("ovid2_pipelines.demo_sf.pipeline_settings")
    settings.DEFAULT_PIPELINE_DB_NAME = "CHAR_DEMO"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ovid2_pipelines.demo_sf.pipeline_settings", settings)
    return "demo_sf"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (None, ("", "")),
        ("", ("", "")),
        ("SANDBOX", ("SANDBOX", "")),
        ("SANDBOX/PUBLIC", ("SANDBOX", "PUBLIC")),
        ("/SANDBOX/PUBLIC/", ("SANDBOX", "PUBLIC")),
    ],
)
def test_split_snowflake_path(path: str | None, expected: tuple[str, str]) -> None:
    assert split_snowflake_path(path) == expected


def test_path_database_beats_pipeline_default(demo_pipeline: str) -> None:
    merged = _resolve_snowflake_config({"path": "SANDBOX"}, None, demo_pipeline)
    assert merged["database"] == "SANDBOX"
    assert "schema" not in merged


def test_path_schema_is_lifted(demo_pipeline: str) -> None:
    merged = _resolve_snowflake_config({"path": "SANDBOX/PUBLIC"}, None, demo_pipeline)
    assert (merged["database"], merged["schema"]) == ("SANDBOX", "PUBLIC")


def test_explicit_database_beats_path(demo_pipeline: str) -> None:
    merged = _resolve_snowflake_config(
        {"path": "SANDBOX", "database": "OTHER"}, None, demo_pipeline
    )
    assert merged["database"] == "OTHER"


def test_pipeline_default_still_applies_without_path(demo_pipeline: str) -> None:
    merged = _resolve_snowflake_config({}, None, demo_pipeline)
    assert merged["database"] == "CHAR_DEMO"


def test_cli_override_still_wins(demo_pipeline: str) -> None:
    merged = _resolve_snowflake_config({"path": "SANDBOX"}, {"database": "CLI_DB"}, demo_pipeline)
    assert merged["database"] == "CLI_DB"
