---
title: Privacy & identifiers
summary: How Ovid2 protects identifiers and row-level data — hashing, tokens, scoped secrets and approval gates.
audience: [researcher, admin]
---

> [!CAUTION]
> Ovid2 processes protected health information. These controls *reduce*
> risk; they don't replace your institution's data-use agreements,
> de-identification review or access policies.

## Identifiers in the output

| Mechanism | What it does |
| --- | --- |
| `hash_id` mapper op | deterministically hashes an identifier column with a **per-pipeline salt** stored in `secrets.d/<pipeline>.json`. The same input always gives the same output within that pipeline; without the salt the hash can't be reversed or reproduced. |
| `HASH_TOKEN` | privacy-preserving record-linkage tokens (e.g. from Datavant). They match patients across datamarts without exchanging identities. |
| Address history | `LDS_ADDRESS_HISTORY` keeps only Limited Data Set fields. Full addresses stay in private tables, if any. |

## Who can see rows

| Actor | Metadata (names, sizes, columns) | Row values |
| --- | --- | --- |
| viewer | runs and reports | — |
| pipeline operator | ✓ | ✓ (Data Space sample and export, audited) |
| system admin | ✓ | ✓ |
| AI agent (MCP, assistant) | ✓ | only after a **single-use human approval per table** |

Every export and every agent row access is written to the audit log.

## What never leaves the executor

- **Secrets.** Only executor subprocesses read them, and each one sees only
  its own scope.
- **Full-table exports** run in a container. They never pass through the
  web process.
- **The assistant** receives tool results such as logs, configs and
  metadata. Row-level endpoints are excluded from its tools. Its
  conversation stays in your browser, and only metadata is audited. Don't
  paste PHI into the chat.

## Retention

Event logs are kept 30 days and job records 90 days by default
(*Settings → Retention*). Quick-download export files are deleted after one
hour.
