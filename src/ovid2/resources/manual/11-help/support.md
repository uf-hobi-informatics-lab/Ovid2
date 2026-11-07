---
title: Support & resources
summary: Where to report problems, request features, read the change history, and learn more.
audience: [user, researcher, developer, admin]
---

## Report a problem

Include:

- the **instance id**;
- `ovid2 --version`;
- the engine and the storage types involved;
- the relevant lines from the run's *Console*, or from `events-<id>.jsonl`.

> [!CAUTION]
> Never include credentials, PHI or real patient data in an issue, a
> chat, or a log you share. Strip row values from any excerpt.

| Channel | Use for |
| --- | --- |
| The project's issue tracker | bugs, feature requests, documentation fixes |
| `SECURITY.md` | how to report a vulnerability **privately** |
| `CONTRIBUTING.md` | how to propose a change |

## Change history

- `CHANGELOG.md` in the repository is the running log of every change.
- `ovid2 --version` prints the installed version. Ovid2 is pre-1.0, so
  check the changelog before upgrading.

## Learn more

| Topic | Where |
| --- | --- |
| PCORnet CDM | the PCORnet Common Data Model specification (v7.0) |
| OMOP CDM | the OHDSI Common Data Model documentation (v5.3) |
| OHDSI vocabularies | the OHDSI Athena vocabulary service |
| Model Context Protocol | the MCP specification |
| The REST API, live | `/docs` on your server |
| This manual, offline | `ovid2 manual build --out site/` |

## About

Ovid2 is *the Artisan*, the trusted data engine; Agent Ovid is *the
Steward*, the planner. Both work to one creed: **fidelity over fortune.**
