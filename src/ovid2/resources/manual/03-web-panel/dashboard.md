---
title: Dashboard
summary: The landing page — job queue, recent runs and environment checks at a glance.
audience: [user]
---

The **Dashboard** (`/`) is the first page you see. It refreshes on its
own (every 3 seconds by default; set *Settings → UI → dashboard poll*).

| Card | Shows |
| --- | --- |
| **Jobs total / Queued / Runs tracked** | KPI tiles for the job queue and run history |
| **Queue & recent jobs** | the latest submitted jobs: pipeline, batch, status, submitted time. Click a row to open its run page. |
| **Recent runs (tracker)** | runs recorded by the batch tracker, including ones started outside the panel |
| **Environment checks** | the same checks as `ovid2 doctor`. A red row means something on the host needs attention. |

## Job states

| Status | Meaning |
| --- | --- |
| `draft` | fully specified but **not queued**; waits for you to press ▶ |
| `queued` | waiting for the worker |
| `running` | the worker started it |
| `done` | finished successfully |
| `failed` | exited non-zero. Open the run page for the error. |
| `cancelled` | stopped by a user (SIGTERM, cleaned up by the worker) |

> [!TIP]
> Jobs stuck in **queued** usually mean the worker isn't running. Start
> `ovid2 web-worker`, or `sudo systemctl start ovid2-web-worker` on a
> server.
