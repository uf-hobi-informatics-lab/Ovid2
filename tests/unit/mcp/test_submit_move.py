"""Unit tests for the :mod:`ovid2.mcp.tools.submit_move` MCP tool.

Locks the invariants the M1 MCP surface has to hold:

- Metadata (``TOOL_NAME``, ``TOOL_DESCRIPTION``, ``ARGS_MODEL``) are
  the shapes the discovery harness expects.
- ``run()`` posts to ``/api/v1/runs`` with ``kind='move'`` hardcoded
  — even if a mis-authored caller injects a different ``kind``, the
  tool always forces the right value before sending.
- The Pydantic ``ARGS_MODEL`` requires the fields
  ``ovid2 tools move`` needs (pipeline, batch_id, input_address,
  output_address, tables) and refuses the ``['all']`` sentinel *at
  submit time via the server* — the tool itself is a thin wrapper so
  it forwards the list verbatim; server-side ``jobs_svc._validate_submit``
  is where the ``['all']`` rejection happens.
- ``strict_types`` defaults to ``False`` and is forwarded verbatim
  when the caller opts in.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from ovid2.mcp.tools import submit_move as tool


@pytest.mark.unit
def test_tool_name_is_submit_move() -> None:
    assert tool.TOOL_NAME == "submit_move"


@pytest.mark.unit
def test_tool_description_mentions_move_semantics() -> None:
    desc = tool.TOOL_DESCRIPTION.lower()
    assert "tools move" in desc
    assert "strict_types" in desc


@pytest.mark.unit
def test_args_model_requires_tables() -> None:
    """``tables`` is a required field — no default, no ``['all']``."""
    with pytest.raises(ValidationError):
        tool.SubmitMoveArgs(
            pipeline="p",
            batch_id="b",
            input_address="in_test",
            output_address="out_test",
        )


@pytest.mark.unit
def test_args_model_strict_types_default_is_false() -> None:
    args = tool.SubmitMoveArgs(
        pipeline="p",
        batch_id="b",
        input_address="in_test",
        output_address="out_test",
        tables=["t1"],
    )
    assert args.strict_types is False


@pytest.mark.unit
def test_args_model_accepts_extra_mounts() -> None:
    args = tool.SubmitMoveArgs(
        pipeline="p",
        batch_id="b",
        input_address="in_test",
        output_address="out_test",
        tables=["t1"],
        extra_mounts=["/host:/mnt", "/host2:/mnt2:ro"],
    )
    assert len(args.extra_mounts) == 2


@pytest.mark.unit
def test_run_posts_with_kind_move_hardcoded() -> None:
    """``run()`` always sends ``kind='move'`` even without an explicit field."""
    import anyio

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value={"job_id": "j-1", "instance_id": "i-1"})

    args = tool.SubmitMoveArgs(
        pipeline="demo",
        batch_id="20260101",
        input_address="in_test",
        output_address="out_test",
        tables=["hash_token"],
    )

    async def _call() -> None:
        result = await tool.run(fake_client, args)
        assert result == {"job_id": "j-1", "instance_id": "i-1"}

    anyio.run(_call)

    fake_client.post.assert_awaited_once()
    call_args, call_kwargs = fake_client.post.call_args
    assert call_args[0] == "/api/v1/runs"
    payload = call_kwargs["json_body"]
    assert payload["kind"] == "move"
    assert payload["pipeline"] == "demo"
    assert payload["batch_id"] == "20260101"
    assert payload["tables"] == ["hash_token"]


@pytest.mark.unit
def test_run_forwards_strict_types_when_set() -> None:
    """When ``strict_types=True`` on the args, the payload carries it."""
    import anyio

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value={"job_id": "j-2"})

    args = tool.SubmitMoveArgs(
        pipeline="demo",
        batch_id="20260101",
        input_address="in_test",
        output_address="out_test",
        tables=["t1"],
        strict_types=True,
    )

    async def _call() -> None:
        await tool.run(fake_client, args)

    anyio.run(_call)
    payload = fake_client.post.call_args.kwargs["json_body"]
    assert payload["strict_types"] is True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run with pytest.")
