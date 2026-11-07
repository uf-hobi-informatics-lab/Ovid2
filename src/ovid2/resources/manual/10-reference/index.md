---
title: Reference
summary: Exhaustive, generated reference — every command, endpoint, tool, op, adapter, fixer, driver, engine, CDM and setting.
audience: [user, researcher, developer, admin]
---

Pages marked <span class="om-badge om-badge-generated">Generated</span>
are produced from the code by `ovid2 manual gen-reference`, and a contract
test checks on every commit that they match the code.

| Reference | Covers | Source of truth |
| --- | --- | --- |
| [CLI](cli.md) | every `ovid2` command, subcommand and flag | the argparse tree |
| [REST API](api.md) | every `/api/v1` operation | the OpenAPI schema |
| [MCP tools](mcp-tools.md) | every Agent Ovid tool and its arguments | `ovid2.mcp.tools.*` |
| [Run kinds](run-kinds.md) | job types and their behaviour | `ovid2.run_kinds` |
| [Transforms](transforms.md) | every transform op and its parameters | the transforms catalogue |
| [Mapper ops](mapper-ops.md) | every mapper op and its parameters | `ovid2.mappers.registry` |
| [Formatter adapters](adapters.md) | every adapter | `ovid2.formatters.adapters` |
| [Fixers](fixers.md) | every common fixer, by scope | the fixer registry |
| [Drivers & engines](drivers-engines.md) | storage types and compute engines | the connection and engine registries |
| [CDMs & vocabularies](cdms.md) | tables, keys, fields, vocabularies | the CDM and semantic YAML |
| [Environment variables](environment.md) | every `OVID2_*` and XDG variable | hand-maintained |

Interactive equivalents on a running server: `/docs` (Swagger UI),
`/redoc`, `/openapi.json`, `ovid2 <command> --help`, and
`GET /api/v1/transforms`.
