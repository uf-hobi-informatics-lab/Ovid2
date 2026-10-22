# Ovid2 installer guide

Ovid2 is a standard PEP-621 Python package. Everything below assumes you have
Python 3.11+ and either `pipx`, `uv`, or a plain `pip` install available.

## 1. Install the package

```bash
# recommended (workstation)
pipx install ovid2

# or as an isolated tool with uv
uv tool install ovid2

# or in a project virtualenv
pip install ovid2
```

Two console scripts land on `$PATH`:

- `ovid2` — main entry point (`etl`, `analysis`, `dq_checks`, `scheduler`, `watch`, …).
- `ovid2-watch` — shortcut to `ovid2 watch --follow-latest`.

## 2. Bootstrap the config tree

Run `ovid2 init` once per user account. It creates the XDG directories and
drops sample configs plus a Snowflake secret template:

```bash
ovid2 init                # non-interactive: writes REPLACE_ME templates
ovid2 init --interactive  # prompts for Snowflake creds on stdin
```

Resulting layout:

```
$XDG_CONFIG_HOME/ovid2/            # ~/.config/ovid2 by default
    configs/
        disc_input.json
        disc_output.json
        snowflake_sample.json
    secrets.d/
        snowflake.json             # mode 0600
$XDG_CACHE_HOME/ovid2/             # ~/.cache/ovid2
$XDG_STATE_HOME/ovid2/             # ~/.local/state/ovid2 (scheduler.db, archive)
$XDG_RUNTIME_DIR/ovid2/            # /run/user/<uid>/ovid2 (event logs, temp dirs)
```

## 3. Verify with `ovid2 doctor`

The doctor runs a set of read-only checks and prints a report:

```bash
ovid2 doctor
```

Exit code is non-zero if any check has severity `error`. Warnings (e.g.
optional dependency missing, no CDM CSV present) are informational.

Checks performed:

| Check | Meaning |
| --- | --- |
| `python` | Interpreter is 3.11+. |
| `import:*` | Runtime dependencies importable. |
| `dir:*` | XDG directories exist and are writable. |
| `configs` | Address configs present under `configs/`. |
| `secret:*` | Every `secrets.d/*.json` is mode 0600. |
| `docker` | Docker CLI on PATH and daemon reachable. |
| `image` | The configured `ovid2:*` image is available locally. |
| `systemd` | `ovid2-scheduler.service` unit is registered. |
| `cdm` | PCORnet CDM CSV is present for formatter validation. |

## 4. Build the Docker image (optional)

```bash
ovid2 image build --tag ovid2:latest
ovid2 image push  --tag ovid2:latest
```

The default tag comes from `$OVID2_DOCKER_IMAGE`.

## 5. Install as a systemd service

The scheduler daemon can run under systemd. Install with root privileges:

```bash
sudo ovid2 service install --user ovid --bin /usr/local/bin/ovid2
sudo systemctl enable --now ovid2-scheduler.service
```

This drops three files:

- `/etc/systemd/system/ovid2-scheduler.service`
- `/etc/logrotate.d/ovid2`
- `/etc/tmpfiles.d/ovid2.conf`

The service hardening block enables `NoNewPrivileges`, `PrivateTmp`,
`ProtectSystem=full`, etc. The unit reads `XDG_STATE_HOME=/var/lib` and
`XDG_RUNTIME_DIR=/var/run` so a system deployment uses the standard
FHS paths for persistent state and event logs.

Common operations:

```bash
sudo ovid2 service status
sudo ovid2 service restart
sudo ovid2 service logs -f
sudo ovid2 service uninstall
```

## 6. Secrets layout

Every JSON file under `secrets.d/` must be mode `0600` and owned by the
running user. The loader (`ovid2.config.secrets.load_secret`) refuses to
open group-readable or world-readable files.

Known secret names:

- `snowflake.json` — used by `SnowflakeAddress` and by the scheduler's
  batch tracker.
- `datavant.json` — used by `datavant` upload utilities (P8).

## 7. XDG environment variables

Ovid2 honours every relevant XDG variable:

| Variable | Default | Purpose |
| --- | --- | --- |
| `XDG_CONFIG_HOME` | `~/.config` | Address configs + secrets. |
| `XDG_CACHE_HOME` | `~/.cache` | Reference tables cache. |
| `XDG_STATE_HOME` | `~/.local/state` | Scheduler DB, event log archive. |
| `XDG_RUNTIME_DIR` | `/run/user/<uid>` (falls back to `/tmp/ovid2-<uid>`) | Event logs, compose files. |

You can also point Ovid2 at a fully custom tree with these overrides:

- `OVID2_MATERIAL_DIR` — replaces `/app/material`.
- `OVID2_DATA_DIR` — replaces `/data`.
- `OVID2_WORK_DIR` — replaces `$XDG_RUNTIME_DIR/ovid2`.
- `OVID2_SECRETS_FILE` — single-file secret override (used in CI).
