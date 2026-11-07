---
title: Logs & monitoring
summary: Where every log lives, how to follow runs, and what the audit trail records.
audience: [admin]
---

## Where to look

| Source | Location | Contents |
| --- | --- | --- |
| Service journals | `journalctl -u ovid2-web -u ovid2-web-worker -u ovid2-mcp` | process logs, start-up errors, scope warnings |
| Run event logs | `/var/run/ovid2/events-<instance_id>.jsonl` | the structured event stream of every run |
| Job consoles | the run page's *Console*; files in the web log dir (`OVID2_WEB_LOG_DIR`) | subprocess stdout and stderr |
| Event archive | the archive dir (`OVID2_ARCHIVE_DIR`), one JSONL per day | long-term event history |
| Audit log | the `web_audit` table in `scheduler.db` | who did what, when, and the outcome |
| Rotated files | `/var/log/ovid2/` | managed by `/etc/logrotate.d/ovid2` |

## Health of the host

- **System → Health** (`/health`) shows the same diagnostics as
  `ovid2 doctor`, with severity, message and hint.
- `GET /api/v1/health` returns them as JSON, for external monitoring.
- The **Dashboard** shows queue depth and recent failures.

## What is audited

Every mutating API call, plus these events:

| Event | Records |
| --- | --- |
| job submit, cancel, rerun, launch, discard | actor, job, kind, pipeline |
| configuration changes | actor, resource, action |
| `dataspace.export`, `dataspace.export_job` | actor, table, format, row limit |
| approvals | requester, action, target, decision, approver |
| `llm.chat` | actor, route, model, token counts, latency, tool names (never prompt or answer text) |
| sign-in, sign-out, role changes | user, provider, outcome |

## Live viewing

```bash
ovid2 watch --follow-latest
tail -f /var/run/ovid2/events-*.jsonl | jq -c '{kind, table, error}'
```
