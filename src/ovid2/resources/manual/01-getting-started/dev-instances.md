---
title: Dev instances on a shared server
summary: Give every developer a private web panel and worker with its own port, state, cookie and image tag.
audience: [developer]
---

When several people share one server, each runs their own Ovid2 with
`ovid2 dev`. Instances never share ports, state, session cookies or Docker
image tags, either with each other or with the production deployment.

## Quick start

```bash
ovid2 dev up                       # start instance "default"; prints its URL
ovid2 dev up feature-x --mcp       # a second instance, plus an MCP server
ovid2 dev ls                       # your instances
ovid2 dev ls --all                 # every instance on the host
ovid2 dev logs feature-x worker -f
ovid2 dev restart                  # pick up code changes
ovid2 dev down feature-x           # stop and release the ports
ovid2 dev down feature-x --purge   # ...and delete its state
```

To run CLI commands against an instance, load its environment first:

```bash
eval "$(ovid2 dev env feature-x)"
ovid2 image build                  # builds ovid2:dev-<you>, never the shared tag
```

## What is isolated

| Concern | Per instance |
| --- | --- |
| Web / MCP port | a leased pair from `8100-8199` (web even, MCP = web + 1) |
| State (jobs, audit, users, approvals) | `~/.local/share/ovid2-dev/<name>/state/` |
| Config, connections, engines, pipelines, settings, secrets | `<root>/config/ovid2/` (fresh from `ovid2 init`) |
| Work dir, archive, cache | `<root>/run/`, `<root>/state/ovid2/archive`, `<root>/cache/` |
| Session cookie | `ovid2_session_<user>_<name>` |
| Docker image tag | `ovid2:dev-<user>` (seeded from `ovid2:latest`) |
| Process logs | `<root>/logs/{web,worker,mcp}.log` |

Input data under `OVID2_DATA_DIR` / `OVID2_MATERIAL_DIR` is intentionally
shared.

Port leases live in a host-wide directory (`/var/tmp/ovid2-dev/ports/`).
Claims are serialised with `flock`. A lease whose processes have all died
is reclaimed automatically.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `OVID2_DEV_HOST` | the host's LAN address | bind address (`127.0.0.1` for SSH-tunnel only) |
| `OVID2_DEV_PORT_RANGE` | `8100-8199` | inclusive port range |
| `OVID2_DEV_HOME` | `~/.local/share/ovid2-dev` | where instance trees live |
| `OVID2_DEV_REGISTRY` | `/var/tmp/ovid2-dev` | shared lease registry |
| `OVID2_DEV_BASE_IMAGE` | `ovid2:latest` | image the per-developer tag is seeded from |

> [!CAUTION]
> A dev instance listens on the LAN with auth off, so teammates can reach
> it and use **your** connections. Before adding credentials you care
> about, either bind it with `--host 127.0.0.1` and use an SSH tunnel, or
> turn on auth for the instance (`eval "$(ovid2 dev env)"; ovid2 web
> reset-break-glass`, then set `auth.enabled` in its `settings.json`).
