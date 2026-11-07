---
title: Scheduling
summary: Run pipeline configs on a cron schedule, at an interval, or whenever new data lands.
audience: [user, admin]
---

Each saved pipeline config has one **trigger**. The **worker**
(`ovid2 web-worker`) evaluates triggers on every tick.

| Trigger | Fires | Settings |
| --- | --- | --- |
| `manual` | only when someone clicks **run**, or calls the API | — |
| `cron` | on a crontab schedule | `cron`, e.g. `0 2 * * 1-5` (02:00 on weekdays) |
| `interval` | every *N* minutes after the last fire | `interval_minutes` ≥ 1 |
| `watch` | once per **new child folder or schema** under the input | `watch_min_wait_minutes` (debounce) |

Cron expressions are validated with `croniter` when you save. The
Pipelines page shows each config's last and next fire times.

## Watch triggers

A watch config keeps an eye on its **bound input** location. When a new
child folder appears, the worker:

- submits one job that reads *exactly* that folder;
- labels it with that folder's full path as the batch id;
- remembers the folder, so it never fires twice.

```text
input address: inbox/landing/
  ├── 2026_01/   (seen)
  ├── 2026_02/   (seen)
  └── 2026_03/   ← new → job with batch id inbox/landing/2026_03
```

Rules:

- The input must be bound (not open), and its storage type must support
  listing.
- A watch config writes to one fixed output. Tick **Runs may overwrite
  existing output data** (`allow_overwrite`), or the second batch stops at
  the overwrite gate.
- `watch_min_wait_minutes` stops the worker re-scanning the same input
  more often than that.

## Turning scheduling off

*Settings → Worker → route dispatch* (`route_dispatch_enabled`) pauses
every cron, interval and watch trigger at once. Manual runs still work.
To pause one config, **disable** it on the Pipelines page.

## Running the worker

```bash
ovid2 web-worker --max-parallel 2     # foreground
sudo systemctl enable --now ovid2-web-worker
```

`--max-parallel` caps how many jobs run at the same time. Drafts are never
picked up until a human launches them.
