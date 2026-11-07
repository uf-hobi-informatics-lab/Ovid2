---
title: Administration — users & approvals
summary: Activate users, assign roles, and handle pending approval requests.
audience: [admin]
---

These pages matter only when authentication is enabled. See
[Authentication & roles](../08-operations/auth.md).

## Users

**Access → Users** (`/users`, needs `manage_users`) lists every account:
name, e-mail, role, status and last login. New SSO sign-ins appear under
**Pending approvals** at the top.

| Action | Effect |
| --- | --- |
| **Activate** (choose a role) | the user can sign in and gets the role's capabilities |
| **Change role** | takes effect on the user's next request |
| **Disable / Enable** | blocks or restores access without deleting history |

The last active `system_admin` can't be demoted or disabled; the panel
returns `409` so you can't lock yourself out.

### Roles and capabilities

| Capability | guest | viewer | operator | admin |
| --- | :-: | :-: | :-: | :-: |
| View CDMs, health, this manual, API docs | ✓ | ✓ | ✓ | ✓ |
| View runs and reports; use the assistant | | ✓ | ✓ | ✓ |
| View pipeline data, submit and cancel jobs | | | ✓ | ✓ |
| Manage config, secrets, users, LLMs, settings, install | | | | ✓ |

## Approvals

Gated operations create an **approval request**. Examples are a
destructive delete, an agent asking to sample rows, and an agent
confirming an overwrite. The requester sees *awaiting approval*.

A `system_admin` reviews pending requests through the API:

```bash
curl -s http://127.0.0.1:8090/api/v1/approvals                 # list
curl -s -X POST http://127.0.0.1:8090/api/v1/approvals/<id>/approve
curl -s -X POST http://127.0.0.1:8090/api/v1/approvals/<id>/deny
```

Each approval is **single-use** and covers exactly the target it names. See [Approvals](../07-assistant/approvals.md).

## Recovery from the host

If nobody can sign in, an operator on the host can use these commands.
They don't need the web server to be running:

```bash
ovid2 web grant-admin you@example.org
ovid2 web list-admins
ovid2 web set-role user@example.org viewer
ovid2 web reset-break-glass        # sealed local admin password
```
