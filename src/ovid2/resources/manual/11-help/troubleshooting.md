---
title: Troubleshooting
summary: Symptoms and error messages, their causes, and the fix — organised by where you see them.
audience: [user, developer, admin]
---

> [!TIP]
> First, find the run's **instance id**. Its run page (*Console* and
> *Timeline*) and its event log, `events-<id>.jsonl`, usually name the
> cause.

## Submitting

| Symptom / message | Cause | Fix |
| --- | --- | --- |
| Job stays **queued** | the worker isn't running | start `ovid2 web-worker` / `systemctl start ovid2-web-worker` |
| Job stays **queued** while another job runs | the worker runs at most `OVID2_WEB_MAX_PARALLEL_JOBS` jobs at once (default `1`); the rest wait, oldest first | wait, or raise the cap: set `OVID2_WEB_MAX_PARALLEL_JOBS` (or `ovid2 web-worker --max-parallel <n>`) and restart the worker |
| `409 overwrite_required` / *Overwrite existing data?* | the output already holds tables | confirm, or choose a new output. See [Overwrite](../04-running-jobs/overwrite-and-batches.md). |
| *Choose a database/schema … a warehouse connection has no runnable root* | you picked a warehouse account root | browse into a database and schema |
| Warehouse output *names only a database* | outputs must be `DATABASE/SCHEMA` | pick or create a schema |
| *Pipeline '<name>' is not installed under ovid2_pipelines* | an `etl` / `analysis` package isn't on `PYTHONPATH` or in the image | install the package, or set `OVID2_PIPELINE_PACKAGES_DIR`, then rebuild the image |
| *watch triggers need a bound input storage account* | watch on an open input | bind the input, or use a manual or cron trigger |
| Engine and driver refused at plan time | e.g. a DuckDB-only driver with `pyspark`, or `REQUIRES_FAMILY` | change the engine. See [Engines](../02-concepts/engines.md#engine-and-storage-compatibility). |

## Running

| Symptom / message | Cause | Fix |
| --- | --- | --- |
| *No files found* / an empty table | the input address doesn't point at the folder holding the files, or a glob doesn't match | point the address at the exact folder; check `sources.*.glob` |
| A code change has no effect | the image wasn't rebuilt | `ovid2 image build` |
| *No Snowflake database specified* | a Snowflake address without a database | set `DATABASE/SCHEMA` on the address |
| `SecretNotFoundError … The run was not granted it` | `enforce` mode, and a secret outside the job's scope | add the `secret_ref` to the connection or engine the job uses |
| *Secret 'x' is outside this executor's declared scope* (warning) | the same, in `warn` mode | fix it before switching to `enforce` |
| `fix.failed` in the timeline | a fixer raised; the table kept its pre-fix data | read the error; check the fixer parameters in `fixer_configs.py` |
| Many duplicate-looking rows remain | they differ on the composite key, or the key is null | check key columns; null-key rows are deliberately kept |
| Spark out of memory | too few workers, or a skewed key | raise `--workers`, try `--engine duckdb`, or raise `OVID2_DEDUP_SALT_BUCKETS` |
| `hash_id: no salt found` (warning) | `secrets.d/<pipeline>.json` is missing `id_salt` | create the salt **before** the first production run |
| `docker: could not find an available, non-overlapping IPv4 address pool` (or *all predefined address pools have been fully subnetted*) | every run gets its own `ovid2-net-<instance id>` network, and Docker's default address pools hold only about 31; runs that crashed or were killed before teardown leak theirs | the next run reaps empty `ovid2-net-*` networks older than 6 hours. To free them now: `docker network ls \| grep ovid2`, then `docker network prune` (removes every unused network, not only Ovid2's). |

## Web panel

| Symptom | Cause | Fix |
| --- | --- | --- |
| Redirected to `/login` repeatedly | the session expired, or cookies are blocked | sign in again; check `trust_forwarded_proto` behind a proxy |
| *No admin configured* on `/login` | auth is on with no admin | `ovid2 web grant-admin <email>` |
| A signed-in user sees nothing | the user is still `pending` | an admin activates them under Users |
| The live canvas stops updating | the proxy buffers SSE | `proxy_buffering off`, plus a long read timeout |
| A connection test fails, but the CLI works | the panel's executor lacks the SDK extra, or the secret's mode isn't 0600 | install the extra; `chmod 600` |
| PDF / Word export *not available* | the `export` extra is missing | `pip install "ovid2[export]"` |
| Access tables don't list | the `access` extra or UCanAccess jars are missing | install them and set `OVID2_UCANACCESS_JARS` |
| The assistant's answers get worse, or it forgets earlier details, in a long chat | the session is close to the model's context window (*context rot*); the context meter turns amber or red | press **New session**. To carry on, first ask for a short summary and paste it into the new session. See [Sessions, history & context](../07-assistant/sessions.md#the-context-meter). |
| The assistant history is shared with other users | sign-in is off, so everyone is the same `anonymous` user | [enable sign-in](../08-operations/auth.md) to give each person their own history. See [Who can see my sessions](../07-assistant/sessions.md#who-can-see-my-sessions). |

## Diagnosing the host

```bash
ovid2 doctor                                   # environment checks
journalctl -u ovid2-web -u ovid2-web-worker -n 200
docker ps -a --filter name=ovid2-              # leftover containers
docker network ls | grep ovid2                 # leftover per-run networks
jq -c 'select(.kind|test("failed|error"))' "$XDG_RUNTIME_DIR"/ovid2/events-<id>.jsonl
```
