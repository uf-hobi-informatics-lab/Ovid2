# Dev instances (`ovid2 dev`)

When several developers share one server, each runs their own Ovid2
(web panel + job worker, optionally the MCP server) with `ovid2 dev`.
Instances never share ports, state, session cookies or Docker image tags
with each other or with the production deployment.

## Quick start

```bash
# once, in your own checkout
uv sync --extra dev --extra spark --extra web --extra mcp   # or pip install -e ".[dev,spark,web,mcp]"

ovid2 dev up                 # start instance "default"; prints its URL
ovid2 dev up feature-x --mcp # a second instance (+ MCP HTTP server)
ovid2 dev ls                 # your instances
ovid2 dev ls --all           # every instance on the host (all users)
ovid2 dev logs feature-x worker -f
ovid2 dev restart            # pick up code changes in the web/worker
ovid2 dev down feature-x     # stop and release the ports
ovid2 dev down feature-x --purge   # ... and delete its state
```

Run CLI commands (`ovid2 etl`, `ovid2 image build`, `ovid2 web create-user`,
…) against an instance by loading its environment first:

```bash
eval "$(ovid2 dev env feature-x)"
ovid2 image build            # builds ovid2:dev-<you>, never the shared tag
```

The instance runs whatever `ovid2` you invoked it with, i.e. your own
checkout's editable install.

## What is isolated

| Concern | Per instance |
|---|---|
| Web / MCP port | leased pair from `8100-8199` (web even, MCP = web + 1) |
| State (`scheduler.db`, jobs, audit, users, approvals) | `~/.local/share/ovid2-dev/<name>/state/` |
| Config, connections, engines, LLMs, pipelines, `settings.json`, `secrets.d` | `<root>/config/ovid2/` (fresh from `ovid2 init`) |
| Work dir (compose files, event logs), archive, cache | `<root>/run/`, `<root>/state/ovid2/archive`, `<root>/cache/` |
| Session cookie | `ovid2_session_<user>_<name>` |
| Docker image tag | `ovid2:dev-<user>` (seeded from `ovid2:latest` via `docker tag`) |
| Process logs | `<root>/logs/{web,worker,mcp}.log` |

Run containers are already isolated per run (`ovid2-*-<instance-id>`).
Input data under `OVID2_DATA_DIR` / `OVID2_MATERIAL_DIR` is intentionally
shared.

## Port leases

Leases live in a host-wide sticky directory (`/var/tmp/ovid2-dev/ports/`)
so all accounts see each other's claims. A claim is serialised with
`flock` and only succeeds when the port pair is also bindable. A lease
whose processes are all dead is reclaimed automatically, so a crash or a
reboot never leaks a port.

## Settings

| Env var | Default | Meaning |
|---|---|---|
| `OVID2_DEV_HOST` | host's primary LAN address | Bind address (`--host` per run; `127.0.0.1` for SSH-tunnel only) |
| `OVID2_DEV_PORT_RANGE` | `8100-8199` | Inclusive port range |
| `OVID2_DEV_HOME` | `~/.local/share/ovid2-dev` | Where instance trees live |
| `OVID2_DEV_REGISTRY` | `/var/tmp/ovid2-dev` | Shared lease registry |
| `OVID2_DEV_BASE_IMAGE` | `ovid2:latest` | Image the per-dev tag is seeded from |

## Security note

Auth is off by default, and a dev instance listens on the LAN address so
teammates can reach it. Anyone on the network can use it, with *your*
connections. Use `--host 127.0.0.1` (plus an SSH tunnel), or enable auth in
the instance (`eval "$(ovid2 dev env)"; ovid2 web reset-break-glass`, then
turn `auth.enabled` on in its `settings.json`), before adding credentials
you care about.
