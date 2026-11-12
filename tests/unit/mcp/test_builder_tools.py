"""Unit tests — pipeline-builder MCP tools + fixed pipeline-package paths."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

NEW_TOOLS = {
    "inspect_pipeline_template",
    "check_pipeline_bindings",
    "list_transform_library",
    "save_transform",
    "assign_transform",
}


def test_builder_tools_discovered() -> None:
    from ovid2.mcp import tools as tools_pkg

    names = {s.name for s in tools_pkg.discover_tools()}
    assert names >= NEW_TOOLS


def test_package_tools_use_pipeline_packages_paths() -> None:
    """Package / formatter / mapper tools must not hit the config routes."""
    root = Path(__file__).resolve().parents[3] / "src" / "ovid2" / "mcp" / "tools"
    offenders = []
    for path in root.glob("*.py"):
        if path.stem.endswith("_config") or path.stem.endswith("_configs"):
            continue
        if "/api/v1/pipelines" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert not offenders, offenders


def test_tools_round_trip_against_api(mcp_client, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import anyio

    from ovid2.mcp.tools import (
        get_formatter,
        inspect_pipeline_template,
        list_pipelines,
        list_transform_library,
        save_transform,
    )

    monkeypatch.setenv("OVID2_TRANSFORMS_DIR", str(tmp_path / "lib"))

    async def _call() -> None:
        pkgs = await list_pipelines.run(mcp_client)
        assert any(p["name"] == "example_pcornet" for p in pkgs)
        fmt = await get_formatter.run(
            mcp_client,
            get_formatter.GetFormatterArgs(pipeline="example_pcornet", table="demographic"),
        )
        assert fmt["table"] == "demographic"
        ins = await inspect_pipeline_template.run(
            mcp_client,
            inspect_pipeline_template.InspectPipelineTemplateArgs(
                template="pcornet_etl", name="agent_site"
            ),
        )
        assert any(
            i["key"] == "DATAMARTID" and i["default"].endswith("AGENT_SITE")
            for i in ins["identity"]
        )
        saved = await save_transform.run(
            mcp_client,
            save_transform.SaveTransformArgs(
                name="agent_udf",
                raw_text="v: 1\nsteps:\n  - op: derive_column\n    column: X\n    expr: upper(X)\n",
            ),
        )
        assert saved["action"] == "created"
        lib = await list_transform_library.run(mcp_client)
        assert any(c["name"] == "agent_udf" for c in lib)

    anyio.run(_call)
