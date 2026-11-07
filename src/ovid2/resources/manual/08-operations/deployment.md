---
title: Deployment
summary: Install Ovid2 on a server as systemd services behind a TLS reverse proxy.
audience: [admin]
---

## Services

| Unit | Runs | Required |
| --- | --- | --- |
| `ovid2-web.service` | `ovid2 web` — panel and API (loopback, `127.0.0.1:8090`) | yes |
| `ovid2-web-worker.service` | `ovid2 web-worker` — runs queued and scheduled jobs | yes |
| `ovid2-mcp.service` | `ovid2 mcp --transport http` — Agent Ovid | optional |
| `ovid2-scheduler.service` | legacy in-process scheduler, kept **for rollback only** | no |

The unit templates ship in the package under
`ovid2/resources/systemd/*.service.in`. Each template has two
placeholders: `@USER@` (the service account) and `@OVID2_BIN@` (the
absolute path to `ovid2`). Every unit sets the system XDG layout:

| Variable | Value |
| --- | --- |
| `XDG_CONFIG_HOME` | `/etc` → config in `/etc/ovid2/` |
| `XDG_STATE_HOME` | `/var/lib` → state in `/var/lib/ovid2/` |
| `XDG_RUNTIME_DIR` | `/var/run` → event logs in `/var/run/ovid2/` |

Each unit also reads an optional environment file, `/etc/ovid2/web.env`
or `/etc/ovid2/mcp.env`. The units are hardened (`NoNewPrivileges`,
`PrivateTmp`, `ProtectSystem=full`, `ProtectHome`, kernel protections)
and may write only to `/var/log/ovid2`, `/var/run/ovid2` and
`/var/lib/ovid2`.

## Install

```bash
# 1. Install the package system-wide (as root or into a dedicated venv)
pipx install --global "ovid2[web,mcp]"         # or a venv under /opt

# 2. Base files: tmpfiles.d, logrotate, scheduler unit
sudo ovid2 service install --user ovid2 --bin "$(command -v ovid2)"

# 3. Render the web, worker (and MCP) units from the packaged templates
SRC=$(python -c 'import importlib.resources as r; print(r.files("ovid2.resources.systemd"))')
for u in ovid2-web ovid2-web-worker ovid2-mcp; do
  sed -e "s|@USER@|ovid2|" -e "s|@OVID2_BIN@|$(command -v ovid2)|" \
      "$SRC/$u.service.in" | sudo tee /etc/systemd/system/$u.service >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now ovid2-web ovid2-web-worker   # + ovid2-mcp if wanted

# 4. Bootstrap the config tree as the service user, then build the image
sudo -u ovid2 XDG_CONFIG_HOME=/etc ovid2 init
sudo -u ovid2 ovid2 image build --tag ovid2:latest
```

The **Setup wizard** (`/install`) runs the same checklist from the
browser and shows the exact commands for your host.

## Reverse proxy

The panel deliberately listens on loopback only. Put a TLS-terminating
proxy in front of it and route a hostname to `127.0.0.1:8090`. Two things
to get right:

- **Server-Sent Events.** Disable response buffering for the run event
  streams. In nginx, set `proxy_buffering off` on `/runs/` and
  `/partials/`.
- **Forwarded scheme.** With `auth.trust_forwarded_proto: true` the panel
  trusts `X-Forwarded-Proto`. This matters for secure cookies and OIDC
  redirect URIs.

```nginx
location / {
    proxy_pass http://127.0.0.1:8090;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_buffering off;
    proxy_read_timeout 1h;
}
```

## Host requirements

| Need | Why |
| --- | --- |
| Docker daemon, with the service user in the `docker` group | per-run clusters and containers |
| A fast local scratch disk | Spark and DuckDB spill, staging |
| Read access to the data roots | disc connections are bind-mounted into containers |
| Outbound access to your warehouses and object stores | the drivers |

## Operating the services

```bash
sudo systemctl status ovid2-web ovid2-web-worker
sudo systemctl restart ovid2-web          # after config changes; no image rebuild needed
journalctl -u ovid2-web-worker -f
```
