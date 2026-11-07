---
title: Approvals
summary: The human-in-the-loop gate — which operations need approval, how requests look, and how to grant or deny them.
audience: [admin, user]
---

Some operations can't complete on the strength of a request alone; a
person has to agree first. Ovid2 records each such request as an
**approval**.

## What requires approval

| Action | Triggered when | Target |
| --- | --- | --- |
| `dataspace.agent_row_access` | an **agent** (MCP or assistant) samples or exports rows, or downloads an artifact | `rows:<path>` (one table) |
| `run.overwrite` | an **agent** confirms writing over an occupied output | the output address |
| destructive operations | deleting configs, connections, LLM connections and similar | the resource |

People with the right role are **not** gated on reading rows they may
see. They confirm overwrites interactively instead. See
[Overwrite & batches](../04-running-jobs/overwrite-and-batches.md).

## What the requester sees

```json
HTTP/1.1 202 Accepted
{ "status": "awaiting_approval", "approval_id": "apr_…",
  "action": "dataspace.agent_row_access", "target": "rows:/warehouse/CHAR_DEMO/PUBLIC/DEMOGRAPHIC" }
```

Once the request is approved, the agent retries with the approval id.

## Granting and denying

You need `admin_all` (system admin):

```bash
curl -s  http://127.0.0.1:8090/api/v1/approvals
curl -s  http://127.0.0.1:8090/api/v1/approvals/<id>
curl -sX POST http://127.0.0.1:8090/api/v1/approvals/<id>/approve
curl -sX POST http://127.0.0.1:8090/api/v1/approvals/<id>/deny
```

## Properties

- **Single use.** An approval authorises one operation, once.
- **Narrow.** It covers exactly the action and target it names, such as
  one table.
- **Audited.** The request, the decision and the operation are all logged.
