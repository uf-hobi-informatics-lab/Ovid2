---
title: Configuration
summary: Every configuration file and directory Ovid2 reads, with the environment variables that relocate them.
audience: [admin, developer]
---

## Directory layout

| Path (user default) | System service | Holds | Override |
| --- | --- | --- | --- |
| `~/.config/ovid2/` | `/etc/ovid2/` | all configuration | `XDG_CONFIG_HOME` |
| `…/configs/<name>.json` | | addresses | `OVID2_CONFIGS_DIR` |
| `…/connections/<name>.json` | | storage accounts | `OVID2_CONNECTIONS_DIR` |
| `…/engines/<name>.json` | | compute engines | `OVID2_ENGINES_DIR` |
| `…/llms/<name>.json` | | assistant model connections | `OVID2_LLMS_DIR` |
| `…/pipelines/<name>.json` | | saved pipeline configs | — |
| `…/settings.json` | | panel and worker settings | `OVID2_SETTINGS_FILE` |
| `…/secrets.d/*.json` | | secrets, mode 0600 | see [Secrets](secrets.md) |
| `~/.local/state/ovid2/` | `/var/lib/ovid2/` | `scheduler.db` (jobs, audit, users, approvals), archive | `XDG_STATE_HOME`, `OVID2_SCHEDULER_DB`, `OVID2_ARCHIVE_DIR` |
| `$XDG_RUNTIME_DIR/ovid2/` | `/var/run/ovid2/` | event logs, per-run temp dirs, compose files | `XDG_RUNTIME_DIR`, `OVID2_WORK_DIR` |
| `~/.cache/ovid2/` | | reference caches | `XDG_CACHE_HOME` |
| material | | OMOP vocabulary, geocoding lists, CDM CSVs | `OVID2_MATERIAL_DIR` |
| data | | raw batches (legacy layout) | `OVID2_DATA_DIR` |

If `XDG_RUNTIME_DIR` is unset, Ovid2 falls back to `/tmp/ovid2-<uid>`.

Inside the Docker image, the legacy paths `/app/material`, `/data` and
`/ovid_credentials` are honoured automatically.

## `settings.json`

Edit it on the **Settings** page or by hand, then restart the panel.

```jsonc
{
  "defaults":      { "engine": "pyspark", "workers": 25 },
  "worker":        { "poll_interval_seconds": 2.0, "route_dispatch_enabled": true },
  "ui":            { "dashboard_poll_ms": 3000, "run_events_poll_ms": 2000,
                     "display_timezone": "UTC" },
  "notifications": { "smtp_host": "", "smtp_port": 25, "smtp_from": "", "smtp_to": [],
                     "smtp_secret_ref": null },
  "retention":     { "event_log_days": 30, "web_jobs_days": 90 },
  "fs_picker":     { "enabled": true, "roots": ["/"], "show_hidden": false },
  "ods":           { "default_dimension": "disc" },
  "auth":          { "enabled": false },          // see Authentication & roles
  "llm":           { "enabled": false }           // see Assistant & agents
}
```

> [!TIP]
> Restrict `fs_picker.roots` to the data directories operators actually
> need. It bounds the server-side folder browser that disc connections use.

## Shipped resources (read-only)

| Under `src/ovid2/resources/` | Contents |
| --- | --- |
| `cdms/<cdm>/` | CDM YAML trees |
| `semantic/` | vocabularies and column bindings |
| `health/checks/`, `health/packages/` | check specs and report cycles |
| `transforms/` | shared transform chains |
| `schemas/` | JSON Schemas (formatter, mapper, transform, events, CDM, …) |
| `agent_tools/catalogue.yaml` | the assistant's tool allow-list |
| `systemd/` | unit templates, logrotate, tmpfiles |
| `manual/` | this manual |

Every environment variable: [Environment variables](../10-reference/environment.md).
