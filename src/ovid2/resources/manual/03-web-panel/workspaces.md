---
title: Workspaces
summary: Shared spaces for a team — who sees which pipelines, jobs, reports and credentials, how to switch, and how an administrator sets them up.
audience: [user, admin]
---

A **workspace** is a shared space for one team or project. Its members see
the same:

- saved pipelines and pipeline packages,
- workflows and their runs,
- jobs, runs and logs,
- reports, report sources and templates,
- connections, addresses, secrets, engines and LLM connections,
- the transform library, approvals and audit entries.

Nothing crosses workspaces. Items from another workspace don't appear in
any list or in the Data Space, and opening one by id or URL returns
**404 Not found**, the same as if it didn't exist.

Shipped content is shared by every workspace: the CDMs, packs, health
checks, built-in transforms, built-in report templates and this manual.
Site settings and sign-in are global too.

## Membership and roles

Membership decides **where** you can work. Your role decides **what** you
can do there. A `viewer` who joins a workspace is still a viewer in it:
they can see its runs and reports but can't submit jobs or edit
configuration. See [Administration](administration.md) for the roles.

| | Membership role `member` | Membership role `admin` |
| --- | :-: | :-: |
| See and use the workspace (within your own role) | ✓ | ✓ |
| Add and remove members, change their workspace role | | ✓ |

A workspace `admin` gains nothing beyond managing members. It doesn't
grant any extra capability.

A **system administrator** sees every workspace without being a member,
and is the only one who can create, rename, archive or delete one.

## Switching workspaces

When you belong to more than one workspace, a **workspace menu** appears in
the top bar next to the Ovid2 logo. Choose another workspace and the page
reloads at the same section (Pipelines, Jobs, …) of that workspace. Ovid2
remembers your choice for your next sign-in. With a single workspace, its
name is shown instead of a menu.

## For administrators

**Access → Workspaces** (`/workspaces`, system admins):

1. **Create.** Give a name and a *slug*: 2–40 lowercase letters, digits,
   `-` or `_`. The slug can't change later, because it names the
   workspace's folder and is what API and MCP callers send.
2. **Add members.** On the workspace's page, pick a user and a workspace
   role. Someone who has never signed in isn't listed yet, so ask them to
   sign in once. A user waiting for approval can already be added.
3. **Rename** at any time.
4. **Archive** to hide the workspace from all members without deleting
   anything. **Restore** to bring it back.
5. **Delete** is possible only for an *archived, empty* workspace (no jobs,
   reports or files).

Archive and delete each need approval from a **second** administrator
(`workspace.archive`, `workspace.delete`; see
[Approvals](../07-assistant/approvals.md)).

The **Users** page shows the workspaces each person belongs to. A newly
activated user joins `default` automatically.

### Upgrading

Everything that existed before workspaces is in the **`default`**
workspace, and every active user is a member of it. No file moves:
`default` *is* the existing layout under `$XDG_CONFIG_HOME/ovid2/`. Each
other workspace keeps its own tree:

```
$XDG_CONFIG_HOME/ovid2/workspaces/<slug>/
    connections/ configs/ engines/ llms/ transforms/
    pipelines/ workflows/ secrets.d/ packages/ovid2_pipelines/
```

Pipeline-package names must be unique across **all** workspaces. Creating
a package whose name is already used elsewhere is refused.

## API, CLI and agents

- **REST.** Every `/api/v1` request runs in the caller's active workspace.
  Add `X-Ovid2-Workspace: <slug>` to choose another one you belong to.
  Useful endpoints:
  - `GET /api/v1/me/workspaces`
  - `PUT /api/v1/me/active-workspace`
  - `/api/v1/workspaces…` (see the [REST API reference](../10-reference/api.md))
- **CLI.** `ovid2 --workspace <slug> …` or `OVID2_WORKSPACE=<slug>` makes a
  command use that workspace's connections, addresses, engines, secrets
  and packages. The default is `default`.
- **Assistant.** Each chat turn is pinned to the workspace you chatted
  from. The assistant can't switch workspaces.
- **MCP.** A service token is bound to a list of workspaces, and
  `OVID2_MCP_WORKSPACE` picks one of them. See
  [Authentication & roles](../08-operations/auth.md).
