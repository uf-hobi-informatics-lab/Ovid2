---
title: Monitoring & control
summary: Follow runs live in the browser or terminal, read logs and events, pause, resume, cancel and rerun.
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

## Why is my job queued?

A web, API or MCP job moves through these states:

| State | Meaning | What moves it on |
| --- | --- | --- |
| `draft` | fully specified and validated, but not submitted. The assistant and `POST /api/v1/job-drafts` create drafts. | a person clicks **▶** on the run page (`POST /api/v1/jobs/{job_id}/launch`) |
| `queued` | submitted and waiting for a worker slot | the worker claims it |
| `running` | the worker has started its `ovid2` subprocess | the process exits or is cancelled |
| `done` / `failed` / `cancelled` | finished | — |

The worker (`ovid2 web-worker`) never claims a draft. It claims `queued`
jobs **oldest submission first**, and runs at most
`OVID2_WEB_MAX_PARALLEL_JOBS` of them at a time. The default is **`1`**,
so a newly launched job waits for the whole of any earlier run. While it
waits, the run page says why:

```text
queued — waiting for 1 running job + 2 queued ahead
```

*Running* counts every job currently running. *Queued ahead* counts the
queued jobs that were submitted earlier. When both are zero the page says
`queued — starting…`: the worker picks the job up on its next poll
(every `OVID2_WEB_WORKER_POLL_SECONDS`, default 2 s).

To run more jobs at once, raise the cap and restart the worker:

```bash
ovid2 web-worker --max-parallel 2        # foreground
# or set OVID2_WEB_MAX_PARALLEL_JOBS=2 in the worker's environment
sudo systemctl restart ovid2-web-worker
```

> [!WARNING]
> Each parallel job gets its own containers and, on Spark, its own
> cluster sized for the host. Raise the cap only if the host has room for
> several runs at once.

> [!TIP]
> A job that stays `queued — starting…` for a long time usually means no
> worker is running. Check `systemctl status ovid2-web-worker`, or start
> `ovid2 web-worker` in a terminal.

## Finding the Spark UI

A `pyspark` run in cluster mode starts its own Spark master container,
named `ovid2-master-<instance_id>`. The workers are named
`ovid2-worker-<n>-<instance_id>`. So that parallel runs never collide,
the master's web UI (port 8080) is published on an **ephemeral** host
port, not a fixed one. Ask Docker which port it chose:

```bash
docker ps --filter name=ovid2-master-        # find the master container
docker port ovid2-master-<instance_id> 8080  # e.g. 0.0.0.0:49157
```

Then open `http://<host>:<port>`. The UI disappears when the run ends and
its cluster is removed.

> [!NOTE]
> The application UI (port 4040) runs on the driver container
> (`ovid2-driver-<instance_id>`) and isn't published. DuckDB, Snowpark and
> local-mode Spark (`OVID2_SPARK_MODE=local`) runs have no master, so
> there is no Spark master UI to open. Follow them with the run page or
> `ovid2 watch`.

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

## Pause and resume

A running ETL, copy, move, legacy-port or health run can be **paused at the
next table boundary**. The table that is running finishes first (its write
is already executing); the run then holds, its clocks freeze and its status
reads *paused*. Health and DQ runs hold between checks. One-shot jobs
(exports) cannot pause.

- **Web**: **‖** on the pipeline's card or run console in the
  [Atlas](../03-web-panel/navigator.md#running-pausing-cancelling); **▶**
  resumes with the next table.
- **API**: `POST /api/v1/jobs/{job_id}/pause` and `…/resume`.
- **MCP**: `pause_job` / `resume_job` (proposals for the assistant).

Cancel still works while a run is paused. Pausing and resuming need the
submit-jobs capability.

## Rerun

**Rerun** (or `POST /api/v1/jobs/{job_id}/rerun`) submits an identical
job with a new instance id. Because the output already exists, a rerun
normally needs an [overwrite confirmation](overwrite-and-batches.md).

## E-mail notifications

Fill in *Settings → Notifications* (SMTP host, port, from, to, and a secret
ref for credentials) to receive batch completion digests.
