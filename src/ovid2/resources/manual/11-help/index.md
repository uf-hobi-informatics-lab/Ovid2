---
title: Help
summary: Frequently asked questions, and where to go when something doesn't work.
audience: [user, researcher, developer, admin]
---

- **Something failed?** Go to [Troubleshooting](troubleshooting.md). It
  is organised by symptom and error message.
- **Need a person?** See [Support & resources](support.md).

## Frequently asked questions

### Do I need Docker?

Not for trying Ovid2 out. `ovid2 etl --local` runs in-process, and
`--engine duckdb` needs neither Docker nor Java. Production runs use
Docker so that each run gets its own isolated cluster.

### Which engine should I use?

DuckDB if the batch fits on one host, Snowpark for Snowflake → Snowflake,
PySpark for very large data or Spark-only custom code. See
[Engines](../02-concepts/engines.md).

### Why did my run refuse to write?

The output already holds tables. Addresses are exact locations, so
re-running overwrites. Confirm with `--overwrite`, the web dialog, or
`allow_overwrite` on scheduled configs. See
[Overwrite & batches](../04-running-jobs/overwrite-and-batches.md).

### Where did my `{pipeline}/{batch}` sub-folder go?

Ovid2 never appends anything to an address. Point the address at the exact
folder or schema. The batch id is derived from that path, and is only a
label.

### I changed a formatter, but the run still uses the old one.

Jobs run inside the Docker image, which contains a copy of your code.
Rebuild it: `ovid2 image build`.

### Why does the delivered table have fewer rows than my extract?

Deduplication removes rows with the same key, and fixers remove orphaned
or illogical records. Each fixer logs what it did. See
[Data lineage](../05-data/data-lineage.md).

### Can the AI assistant change or read my data?

It can read metadata and propose actions. Every proposal waits for you to
click **Run**, drafts wait for ▶, and reading rows needs your approval
for each table. It never sees secrets. See
[Assistant & agents](../07-assistant/index.md).

### Where are my assistant chats saved? Can I find an old one?

On the Ovid2 server, in the web database, not in your browser. Every chat
is a saved **session** that survives reloads, other browsers and server
restarts. Open **History** in the assistant panel to reopen one, or
**Search** to find it by any word in its title or messages. Sessions are
kept until you delete them. See
[Sessions, history & context](../07-assistant/sessions.md).

### Why does everyone see the same assistant history?

Sign-in is off, so every visitor is the same `anonymous` user and shares
one history. [Enable sign-in](../08-operations/auth.md) and each person
gets their own. See
[Who can see my sessions](../07-assistant/sessions.md#who-can-see-my-sessions).

### Is the panel secure out of the box?

No. Auth is **off** by default and the panel binds to loopback. Before
exposing it, put a TLS proxy in front and
[enable SSO](../08-operations/auth.md).

### How do I check data quality for a PCORnet database I didn't build?

Run a **Health** job with the `pcornet_edc` label against that database.
You don't need a pipeline package. See
[Health & EDC checks](../04-running-jobs/health-checks.md).

### Can I read this manual offline?

Yes: `ovid2 manual build --out site/`, then open `site/index.html`.
