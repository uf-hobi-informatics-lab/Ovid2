---
title: Maintenance
summary: Back up, upgrade, prune and clean an Ovid2 server safely.
audience: [admin]
---

## Back up

| What | Path (system install) | Why |
| --- | --- | --- |
| Configuration | `/etc/ovid2/` (configs, connections, engines, pipelines, llms, settings) | rebuilding it by hand is slow and error-prone |
| Secrets | `/etc/ovid2/secrets.d/` | **pipeline salts can't be recovered**; losing one changes every hashed id |
| State | `/var/lib/ovid2/scheduler.db` | jobs, audit, users, approvals |
| Your pipeline packages | your git repository | the rules behind every delivery |

Encrypt secret backups, and restore them with mode 0600.

## Upgrade

1. Read the `CHANGELOG.md` entries since your version.
2. Install the new package version, or pull and reinstall your checkout.
3. **Rebuild the image** from the same commit:
   `ovid2 image build --tag ovid2:latest`.
4. `sudo systemctl restart ovid2-web ovid2-web-worker ovid2-mcp`.
5. Run `ovid2 doctor`, then a small known job, and compare its output with
   the previous delivery.

Jobs that are already running keep the image they started with.

## Retention

*Settings → Retention* controls how long event logs (30 days) and job
records (90 days) are kept. Quick-download exports are deleted after one
hour.

### Assistant sessions

Saved [assistant sessions](../07-assistant/sessions.md) are **kept until
their owner deletes them**. No automatic expiry applies. They live in
the web database (`$XDG_STATE_HOME/ovid2/scheduler.db`, tables
`web_llm_sessions` and `web_llm_messages`, plus the search index
`web_llm_messages_fts`), so the normal state backup covers them.

To apply a site retention policy, stop the web service and delete old
sessions with `sqlite3`. For example, to remove sessions untouched for
180 days:

```bash
sqlite3 "$XDG_STATE_HOME/ovid2/scheduler.db" <<'SQL'
CREATE TEMP TABLE old AS SELECT session_id FROM web_llm_sessions
  WHERE updated_at < strftime('%Y-%m-%dT%H:%M:%S', 'now', '-180 days');
DELETE FROM web_llm_messages      WHERE session_id IN (SELECT session_id FROM old);
DELETE FROM web_llm_messages_fts  WHERE session_id IN (SELECT session_id FROM old);
DELETE FROM web_llm_sessions      WHERE session_id IN (SELECT session_id FROM old);
SQL
```

Audit rows for chat turns (`web_audit`, action `llm.chat`) contain only
metadata and follow the normal job-record retention.

## Clean up

- Per-run Docker networks and containers are removed when the run ends.
  If a host crashes, run `docker ps -a --filter name=ovid2-` and
  `docker network ls | grep ovid2` to find leftovers.
- Orphaned per-run files older than six hours are swept automatically.
- Old images: list them with `docker images ovid2`, remove the tags you
  no longer need, then run `docker image prune`.

## Migrating legacy configs

```bash
ovid2 tools migrate-addresses --help    # {src_name, src_args} → {connection, location}
ovid2 migrate pipeline --help           # legacy formatter scripts → specs
```
