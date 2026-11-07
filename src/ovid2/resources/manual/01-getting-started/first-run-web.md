---
title: First run — web panel
summary: Start the control panel and worker, add a storage account, and submit a job from the browser.
audience: [user]
---

## 1. Start the panel and the worker

The panel serves the UI and API. The worker runs queued jobs. You need
both:

```bash
ovid2 web                    # http://127.0.0.1:8090
ovid2 web-worker             # in a second terminal
```

On a server, run both as systemd services instead. See
[Deployment](../08-operations/deployment.md).

> [!CAUTION]
> Authentication is **off by default**: anyone who can reach the port has
> full control. Keep the default loopback bind (`127.0.0.1`), or turn on
> [SSO and roles](../08-operations/auth.md) before you expose the panel.

## 2. Add a storage account

1. Open **Configure → Storage accounts** and click **+ New**.
2. Pick a type (for example *disc* for a local folder or *snowflake* for a
   warehouse), give it a name, and fill in the endpoint fields.
3. Enter the credential in the secret fields. It goes into `secrets.d/`
   (mode 0600), and the browser never sees it again.
4. Click **Test connection**.

Connections are reusable: one account can back many addresses.

## 3. Submit a job

Open **Operate → Submit**. The page has a navigator on the left and a live
job graph on the right.

1. **Job & pipeline:** pick a job type (ETL, Health, Copy …) and a
   pipeline.
2. **Source:** expand your storage account and click the **icon** of the
   folder or schema to read.
3. **Target:** do the same for the output location.
4. **Options** (optional): tables, stages, engine, workers.
5. When every node in the graph is green, click **Submit job**.

The read-only **Batch id** under the graph shows the full path of the
source you chose. More detail: [Submit](../03-web-panel/submit.md).

## 4. Follow the run

You land on the run page (`/runs/<instance_id>`). It shows a live canvas of
tables and stages, a timeline, a console and, when the run finishes, a link
to its report. The **Jobs** page lists every run, with filters.

## 5. Save it for next time

To repeat this job, or put it on a schedule, open **Operate → Pipelines →
+ New pipeline**. That saves a *pipeline config*: kind, pipeline, input,
output, options and trigger. See [Scheduling](../04-running-jobs/scheduling.md).

Next: the [web panel tour](../03-web-panel/index.md) or
[Core concepts](../02-concepts/index.md).
