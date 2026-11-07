---
title: Agent Ovid (MCP)
summary: Run the MCP server, connect a client, authenticate it, and understand its 88 tools.
audience: [developer, admin, user]
---

`ovid2 mcp` is a **Model Context Protocol** server. It exposes Ovid2 to AI
agents as typed tools. Each tool is a thin wrapper over one `/api/v1`
call; the MCP process imports no secret, driver, connection or pipeline
code.

## Run it

```bash
# stdio, for desktop clients that spawn the server
ovid2 mcp --upstream http://127.0.0.1:8090

# streamable HTTP, for remote or shared clients
ovid2 mcp --transport http --bind 127.0.0.1:8091 --upstream http://127.0.0.1:8090
```

As a service: `sudo systemctl enable --now ovid2-mcp`.

### Client configuration (stdio)

```json
{
  "mcpServers": {
    "ovid2": {
      "command": "ovid2",
      "args": ["mcp", "--upstream", "http://127.0.0.1:8090"],
      "env": { "OVID2_MCP_API_TOKEN": "<service token>" }
    }
  }
}
```

## Authenticate

With panel auth enabled, the MCP server sends a **service-principal
bearer token**:

1. Generate a strong random token and compute its SHA-256 hex digest.
2. Map the digest to a role in `secrets.d/auth_service_tokens.json`:
   `{"<sha256hex>": "pipeline_operator"}`.
3. Give the plaintext token to the MCP process as `OVID2_MCP_API_TOKEN`.

The role defaults to `pipeline_operator` and can **never** silently
escalate to `system_admin`.

## Tool families

| Family | Examples |
| --- | --- |
| Jobs & runs | `submit_run`, `submit_health`, `submit_move`, `list_jobs`, `get_job_log`, `run_timeline`, `cancel_job`, `rerun_job` |
| Pipeline configs | `list_pipeline_configs`, `create_pipeline_config`, `run_pipeline_config`, `enable_pipeline_config` |
| Packages & specs | `list_pipelines`, `scaffold_pipeline`, `get_formatter`, `save_formatter`, `validate_formatters`, `get_mapper`, `save_mapper` |
| Storage | `list_connections`, `create_connection`, `test_connection`, `list_addresses`, `storage_browse` |
| Data Space | `ods_list`, `ods_resolve`, `ods_describe_table`, `ods_sample_table` (approval-gated) |
| Engines | `list_engines`, `create_engine`, `test_engine` |
| CDMs & health | `list_cdms`, `get_cdm`, `list_health_checks`, `list_health_packages`, `get_health_report` |
| Transforms | `list_transforms`, `validate_transform` |
| Admin | `list_secrets`, `write_secret`, `get_settings`, `install_status`, `list_approvals` |

The full generated list, with arguments: [MCP tool reference](../10-reference/mcp-tools.md).

## Guard rails

- Every call is authorised by the API against the token's role, and is
  audited.
- **Row values** (`ods_sample_table`, exports, artifacts) return
  `202 awaiting_approval` until a human approves. Each approval is
  single-use and covers one table.
- **Overwrites** become a `run.overwrite` approval.
- **Destructive** operations go through the approval gate.

See [Approvals](approvals.md).
