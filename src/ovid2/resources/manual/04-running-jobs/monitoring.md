---
title: Monitoring & control
summary: Follow runs live in the browser or terminal, read logs and events, cancel and rerun.
audience: [user, admin]
---

## In the browser

`/runs/<instance_id>` shows a run's live canvas, timeline and console.
The **Jobs** page lists every run. See [Jobs & runs](../03-web-panel/jobs.md).

## In the terminal

```bash
ovid2 watch                       # follow the newest run
ovid2 watch --instance <id>       # a specific run
ovid2 watch --follow-latest       # switch to newer runs automatically
ovid2 watch --log path/to/events-<id>.jsonl
```

Leaving `watch` never affects the run.

## Raw events and logs

| What | Where |
| --- | --- |
| Event log for one run | `$XDG_RUNTIME_DIR/ovid2/events-<instance_id>.jsonl` (system installs: `/var/run/ovid2/`) |
| Subprocess console for a web/worker job | the run page's *Console*; files under the web log directory |
| Service logs | `journalctl -u ovid2-web -u ovid2-web-worker -f` |
| Daily event archive | the archive directory, one JSONL per day |

```bash
# Every failed table across today's runs
jq -c 'select(.kind=="table.failed") | {instance_id, table, error}' \
  "$XDG_RUNTIME_DIR"/ovid2/events-*.jsonl
```

## Cancel

- **Web**: click **Cancel** on the run page or in the Jobs list.
- **API**: `POST /api/v1/jobs/{job_id}/cancel`.
- **MCP**: `cancel_job`.

A queued job is cancelled immediately. A running job is sent SIGTERM, and
the worker removes its containers and cluster.

## Rerun

**Rerun** (or `POST /api/v1/jobs/{job_id}/rerun`) submits an identical
job with a new instance id. Because the output already exists, a rerun
normally needs an [overwrite confirmation](overwrite-and-batches.md).

## E-mail notifications

Fill in *Settings → Notifications* (SMTP host, port, from, to, and a secret
ref for credentials) to receive batch completion digests.
