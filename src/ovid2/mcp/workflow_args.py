"""Shared pydantic argument shapes for the workflow MCP tools."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class WorkflowTriggerArgs(BaseModel):
    """What starts the workflow."""

    type: str = Field("manual", description="'manual', 'cron', 'interval' or 'watch'.")
    cron: str | None = Field(None, description="Crontab expression when type='cron'.")
    interval_minutes: int | None = Field(None, description="Minutes between runs (interval).")
    watch_min_wait_minutes: int | None = Field(
        None, description="Debounce between scans of the watched folder (watch)."
    )


class WorkflowOverridesArgs(BaseModel):
    """Per-step overrides of the referenced pipeline config."""

    input_address: str | None = Field(None, description="Address name to read (no data link).")
    output_address: str | None = Field(None, description="Address name to write.")
    tables: list[str] | None = Field(None, description="Table list (copy / move steps).")
    allow_overwrite: bool | None = Field(
        None,
        description=(
            "Standing permission to overwrite an occupied output. Set by an agent "
            "it needs a human run.overwrite approval."
        ),
    )


class WorkflowProbeArgs(BaseModel):
    """A data probe: one aggregate number over the step's output."""

    name: str = Field(..., description="Probe name; conditions read it as 'query.<name>'.")
    table: str = Field(..., description="Table at the step's output.")
    agg: str = Field(
        "count", description="'count', 'count_distinct', 'null_count', 'non_null_count'."
    )
    column: str | None = Field(None, description="Column (required except for 'count').")
    where: str | None = Field(
        None,
        description=(
            "Optional row filter (Spark SQL predicate). Set by an agent it needs a "
            "human workflow.data_probe approval."
        ),
    )


class WorkflowStepArgs(BaseModel):
    """One step."""

    id: str = Field(..., description="Lower-case step id, unique in the workflow.")
    pipeline: str = Field(..., description="Saved pipeline config name.")
    label: str = ""
    rollback: bool = Field(True, description="Undo this step's output if the workflow fails.")
    overrides: WorkflowOverridesArgs = Field(default_factory=WorkflowOverridesArgs)
    join: str = Field(
        "all",
        description=(
            "With several incoming links: 'all' (every link taken) or 'any' "
            "(at least one; use for re-joining branches)."
        ),
    )
    probes: list[WorkflowProbeArgs] = Field(default_factory=list)


class WorkflowEdgeArgs(BaseModel):
    """One link."""

    model_config = {"populate_by_name": True}

    source: str = Field(..., alias="from", description="Upstream step id.")
    target: str = Field(..., alias="to", description="Downstream step id.")
    type: str = Field(
        "data",
        description="'data' — feed the upstream output in as input; 'after' — order only.",
    )
    on: str = Field(
        "success",
        description="'success', 'failure' (recovery path), 'always', or 'otherwise' (else branch).",
    )
    when: dict[str, Any] | None = Field(
        None,
        description=(
            "Condition: {all:[..]} / {any:[..]} / {not:{..}} / {field, op, value}. "
            "Fields and operators: GET /api/v1/workflow-conditions."
        ),
    )
    label: str = ""


class WorkflowBodyArgs(BaseModel):
    """A workflow definition."""

    name: str = Field(..., description="Workflow name (letters, digits, '-', '_', '.').")
    description: str = ""
    enabled: bool = True
    trigger: WorkflowTriggerArgs = Field(default_factory=WorkflowTriggerArgs)
    steps: list[WorkflowStepArgs]
    edges: list[WorkflowEdgeArgs] = Field(default_factory=list)

    def body(self) -> dict[str, object]:
        """Return the JSON body (without the name) for the REST API."""
        return self.model_dump(exclude={"name"}, exclude_none=True, by_alias=True)
