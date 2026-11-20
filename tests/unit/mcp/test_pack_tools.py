"""Unit tests — domain-pack MCP tools against the in-process API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PACK_TOOLS = {"list_packs", "get_pack", "get_repair_context"}


def test_pack_tools_discovered() -> None:
    from ovid2.mcp import tools as tools_pkg

    assert {s.name for s in tools_pkg.discover_tools()} >= PACK_TOOLS


def test_pack_tools_round_trip(mcp_client, isolated_xdg: Path) -> None:  # type: ignore[no-untyped-def]
    import anyio

    from ovid2.mcp.tools import get_pack, get_repair_context, list_packs
    from ovid2.web.services import health_report_svc

    iid = "20260101-000000-abcdef"
    path = health_report_svc.report_path(iid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "instance_id": iid,
                "checks": [
                    {
                        "id": "1.01",
                        "status": "exception",
                        "severity": "blocker",
                        "exception_count": 1,
                        "metrics": [{"exception_flag": 1, "value_str": "ROWVALUE"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    async def _call() -> None:
        packs = await list_packs.run(mcp_client, list_packs.ListPacksArgs())
        assert "pcornet_7_0" in {p["id"] for p in packs}
        one = await get_pack.run(mcp_client, get_pack.GetPackArgs(pack_id="pcornet_7_0"))
        assert "1.01" in one["checks"]
        ctx = await get_repair_context.run(
            mcp_client, get_repair_context.GetRepairContextArgs(instance_id=iid)
        )
        assert ctx["failures"][0]["check_id"] == "1.01"
        assert ctx["failures"][0]["pack"] == "pcornet_7_0"

    anyio.run(_call)
