"""``submit_move`` — enqueue an ``ovid2 tools move`` job.

Dedicated MCP tool for the M1 driver-native move command. Under the
hood this is a ``submit_run`` with ``kind='move'`` — clients that
prefer the generic surface can call ``submit_run`` directly; this
tool exists so LLM callers can discover the move capability by
name (``submit_move``) rather than having to know that ``move`` is
one of the values ``kind`` accepts.

The route through the web server is identical:
``POST /api/v1/runs`` with ``kind='move'``. Same server-side
validation applies: explicit ``tables`` list (no ``all`` sentinel),
no ``transformations``, ``strict_types`` accepted, ``extra_mounts``
accepted.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ovid2.mcp.client import WebApiClient


class SubmitMoveArgs(BaseModel):
    """Arguments for :data:`TOOL_NAME`.

    Superset of the fields ``ovid2 tools move`` accepts on the CLI.
    ``kind`` is not exposed — it is hardcoded to ``move`` in
    :func:`run` — so a mis-authored caller cannot accidentally submit
    a different job kind through this tool.
    """

    pipeline: str = Field(
        ...,
        description="Pipeline name (must exist under ovid2_pipelines).",
    )
    batch_id: str = Field(
        "",
        description=(
            "Optional label. Ignored when the input has a location: the batch id is "
            "always derived as the complete Data Space path of the input "
            "(storage account + folder / DATABASE/SCHEMA). Every address is the "
            "exact location its data is read from / written to — nothing is "
            "appended to it."
        ),
    )
    input_address: str = Field(
        ...,
        description=(
            "Filename stem of the source address config. Passed to ``ovid2 tools move --from``."
        ),
    )
    output_address: str = Field(
        ...,
        description=(
            "Filename stem of the destination address config. Passed to ``ovid2 tools move --to``."
        ),
    )
    tables: list[str] = Field(
        ...,
        description=(
            "Explicit list of tables to move. Move jobs never accept "
            "the ``['all']`` sentinel — the server rejects it "
            "outright. Each entry is either a bare logical name "
            "('HASH_TOKEN') or a 'LOGICAL=SOURCE' split "
            "('HASH_TOKEN=*datavant_person_tokens*'). LOGICAL is "
            "always used verbatim as the destination file stem — "
            "case preserved — so glob metacharacters on the source "
            "side never leak into the destination key. SOURCE, when "
            "omitted, defaults to LOGICAL."
        ),
    )
    engine: str = Field(
        "pyspark",
        description=(
            "Compute engine (default: pyspark). Passed through to ``ovid2 tools move --engine``."
        ),
    )
    strict_types: bool = Field(
        False,
        description=(
            "Toggle the D6.0 advisory CDM type check between warn "
            "(default, False) and raise (True). When True, a "
            "source/target address whose stamped CDM disagrees "
            "aborts the move before any I/O; when False, the "
            "mismatch produces a log warning and the move proceeds."
        ),
    )
    extra_mounts: list[str] = Field(
        default_factory=list,
        description=(
            "Additional bind mounts for the per-invocation container. "
            "Each entry is 'HOST:CONTAINER[:MODE]' where MODE is 'rw' "
            "(default) or 'ro'. Container path must not shadow the "
            "standard mount set (/app, /data, /ovid2-user-config, "
            "/ovid_credentials, /usr/include/rsa)."
        ),
    )
    confirm_overwrite: bool = Field(
        False,
        description=(
            "Set True to let the run overwrite an output location that already "
            "holds tables. Without it an occupied output answers 409 "
            "overwrite_required (listing the existing tables). For an agent this "
            "becomes a run.overwrite approval request (202) a human must approve "
            "before re-submitting."
        ),
    )


TOOL_NAME = "submit_move"
TOOL_DESCRIPTION = (
    "Enqueue an ``ovid2 tools move`` job — the driver-native "
    "successor to ``ovid2 tools copy``. Same address-to-address "
    "one-shot semantics as ``copy`` (bypasses the formatter, mapper, "
    "and fixer stages) with two additions: I/O routes through the "
    "D3 native drivers instead of the pre-refactor ``BaseAddress`` "
    "chain, and a ``strict_types`` flag toggles the D6.0 advisory "
    "CDM type check between warn (default) and raise. Returns the "
    "freshly-created job_id and instance_id."
)
ARGS_MODEL = SubmitMoveArgs


async def run(client: WebApiClient, args: SubmitMoveArgs) -> Any:
    """Call ``POST /api/v1/runs`` with ``kind='move'``."""
    payload = args.model_dump()
    payload["kind"] = "move"
    return await client.post("/api/v1/runs", json_body=payload)
