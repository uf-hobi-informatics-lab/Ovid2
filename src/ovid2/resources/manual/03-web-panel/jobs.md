---
title: Jobs & runs
summary: Job history, the live run page (canvas, timeline, console) and health reports.
audience: [user, researcher]
---

## The Jobs page

**Operate → Jobs** (`/runs`) is the full job history: drafts, queued,
running and finished jobs. Filter by status, pipeline or text. Each row
links to the run page.

## The run page

`/runs/<instance_id>` follows one job. It has these parts.

### Status bar

The status bar shows the job state and its actions:

| Action | Available when | Does |
| --- | --- | --- |
| ▶ **Launch** | draft | queues the draft (pulses when the assistant prepared it) |
| **Discard** | draft | deletes the draft |
| **Cancel** | queued / running | stops the job (running jobs get SIGTERM) |
| ▶ **Rerun** | finished | submits an identical job with a new instance id |
| **Download export** | finished export job | downloads the exported file |

Launch and rerun check the output first. If it already holds tables, an
in-page dialog lists them and asks you to confirm; *Cancel* sends nothing.

### Live canvas

The live canvas shows each table as a node with its stages. Stages light
up as events arrive. Headline metrics: **elapsed**, current **stage**,
**rows written**, **executors**. The canvas streams over Server-Sent Events
from the run's event log, so it can catch up after a reconnect.

### Timeline

The timeline is a table of every CDM table: started and ended times,
status, the stage it reached, fixes applied, and any error.

### Console

The console shows the raw log of the job's subprocess, with the tail
first. It is the place to read a stack trace.

## Health reports

Health and DQ-check jobs add a **report** at
`/runs/<instance_id>/report`. It shows each check's outcome (`pass`,
`warn`, `fail`, `not_applicable`), its threshold and its metrics, grouped
by part. See [Reading a health report](../05-data/reading-health-reports.md).

> [!NOTE]
> Run pages need `view_runs` (viewer and above). Cancel and rerun need
> `submit_jobs`.
