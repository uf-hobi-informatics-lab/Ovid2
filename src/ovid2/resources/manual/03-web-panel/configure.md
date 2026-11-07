---
title: Configure — LLMs, secrets, CDMs, settings, setup
summary: The remaining configuration pages and what each one controls.
audience: [user, admin]
---

## LLMs

**Configure → LLMs** (`/llms`, needs `manage_config`) holds the
assistant's model connections and its routing: general model, System 1
model, confidence threshold, and tools on or off. Full guide:
[Assistant & agents](../07-assistant/index.md).

## Secrets

**Configure → Secrets** (`/secrets`, needs `manage_secrets`) lists every
file in `secrets.d/` with its keys, file mode and modification time. You
can create, update or delete a secret. **Secret values are never
displayed.** The page only shows key names. Forms are CSRF-protected.

## CDMs

**Configure → CDMs** (`/cdms`) lists every registered data model as a
card: origin and number of tables. Open one to browse its tables, fields,
types, keys and valuesets. Everyone can see this page. Researchers:
[Data & data models](../05-data/index.md).

## Settings

**Configure → Settings** (`/settings`, `admin_all`) edits
`$XDG_CONFIG_HOME/ovid2/settings.json`:

| Section | Keys |
| --- | --- |
| Defaults | `engine`, `workers` used when a job doesn't specify them |
| Worker | `poll_interval_seconds`, `route_dispatch_enabled` (cron / interval / watch on or off) |
| UI | `dashboard_poll_ms`, `run_events_poll_ms`, `display_timezone` |
| Notifications | SMTP host, port, from, to, secret ref (batch completion e-mails) |
| Retention | `event_log_days` (30), `web_jobs_days` (90) |
| Admin storage account | where admin artefacts are stored |

Authentication (`auth`) and assistant routing (`llm`) live in the same
file. See [Configuration](../08-operations/configuration.md).

## Setup wizard

**Configure → Setup wizard** (`/install`, `admin_all`) is a checklist for
a fresh server:

1. Bootstrap the XDG tree (same as `ovid2 init`).
2. Save Snowflake credentials.
3. Add storage accounts.
4. Build the Docker image (progress streams on the page).
5. Install the systemd service (shows the commands to run as root).
6. Verify a first pipeline.
7. Ready to run.

Each step shows its status, so you can come back to finish later.
