---
title: Trust model
summary: Who may do what — the agent, API and executor tiers, scoped secrets, and human approval gates.
audience: [admin, developer, user]
---

Ovid2 handles protected health data and can be driven by AI agents. Its
security model makes sure no single component can both *decide* to do
something dangerous and *do* it.

## Three tiers

```text
  Agent Ovid (MCP / assistant)  ──HTTP only──►  ovid2 web (API)  ──spawns──►  executor
  untrusted planner                            trusted gateway               least privilege
  · reads via typed API                        · authorises (RBAC)            · the only tier that
  · proposes actions                           · audits every request           dereferences secrets
  · never sees secrets                         · gates on approvals           · sees only its scope
```

Contract tests enforce these boundaries on every commit:

- the agent tier imports no secret, driver, connection or pipeline module;
- the API tier never reads a plaintext secret in a request handler;
- neither of them imports the LLM runtime or any data SDK.

## Scoped secrets

Every executor subprocess — every job, connection test, storage browse
and Data Space sample — gets a private directory holding **only** the
secrets its request names:

- addresses contribute their connection's secret;
- the engine contributes its own secret and those of any connections it
  references;
- the pipeline contributes its hash salt.

| `OVID2_SECRETS_SCOPE_MODE` | Behaviour |
| --- | --- |
| `warn` (default) | an out-of-scope secret still resolves, with a warning naming it |
| `enforce` | only the scoped directory is searched; containers also get an empty tmpfs over `secrets.d/` |

Roll-out advice: [Secrets](../08-operations/secrets.md).

## Human approval gates

These operations stop and wait for a person:

| Operation | Gate |
| --- | --- |
| Destructive changes (deleting configs, connections, LLM connections…) | `approvals_svc` request |
| An **agent** reading row values (sample, export, artifacts) | `dataspace.agent_row_access`, single use, one table |
| An **agent** confirming an overwrite of existing output | `run.overwrite` |

Humans are never gated on reading rows they are allowed to see. See
[Approvals](../07-assistant/approvals.md).

## Roles

With authentication on, every user has one role. Each role includes all
the capabilities of the one below it:

`guest` ⊂ `viewer` ⊂ `pipeline_operator` ⊂ `system_admin`

New SSO users start as `pending` and can do nothing until an admin
activates them. See [Authentication & roles](../08-operations/auth.md).
