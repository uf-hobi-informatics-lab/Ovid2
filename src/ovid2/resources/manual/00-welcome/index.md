---
title: Welcome to Ovid2
summary: The healthcare-data ETL engine — what it does, who it serves, and where to begin.
audience: [user, researcher, developer, admin]
---

**Ovid2** turns raw clinical extracts — CSV dumps, warehouse schemas,
Access databases — into standard **common data models** (PCORnet CDM 7.0
and OMOP CDM 5.3), checks their quality against the PCORnet EDC
characterisation queries, and delivers the result to wherever your
research team works: a folder, an object store, or a warehouse such as
Snowflake.

It is built around one promise, its creed: ***Fidelity over fortune.***
Every value that leaves Ovid2 can be traced back to the source row and the
declarative rule that produced it. Nothing is guessed, nothing is silently
dropped, and nothing runs that a human did not approve.

## Choose your path

<div class="cards">
  <a class="card" href="../01-getting-started/index.md"><strong>I run data</strong><span>Install, connect your storage, submit a first job and watch it finish.</span></a>
  <a class="card" href="../05-data/index.md"><strong>I use the data</strong><span>Understand the CDMs, how values are mapped and fixed, and how to read a quality report.</span></a>
  <a class="card" href="../06-authoring/index.md"><strong>I build pipelines</strong><span>Describe a new source with YAML formatter, mapper and fixer specs.</span></a>
  <a class="card" href="../09-developers/index.md"><strong>I extend Ovid2</strong><span>Add drivers, engines, CDMs and fixers without breaking the invariants.</span></a>
  <a class="card" href="../08-operations/index.md"><strong>I operate Ovid2</strong><span>Deploy services, enable SSO and roles, scope secrets, upgrade safely.</span></a>
  <a class="card" href="../11-help/index.md"><strong>I am stuck</strong><span>FAQ, troubleshooting by error message, and where to ask.</span></a>
</div>

## Ovid2 in one minute

| You bring | Ovid2 provides | You get |
| --- | --- | --- |
| Raw source files or tables | Declarative **formatters** that shape each CDM table | Standard PCORnet or OMOP tables |
| Local value codes | **Mappers** that translate to standard vocabularies | Codes researchers can query consistently |
| Imperfect data | **Fixers** that repair or remove illogical records — always logged | Cleaner, characterised data |
| A storage account | **Drivers** for disc, S3, Azure Blob, GCS, Snowflake, Databricks, Postgres, SQL Server, Access | Reads and writes at the exact location you choose |
| A compute preference | **Engines**: PySpark, Databricks, Snowpark, DuckDB | The same results whichever engine runs |
| A question | A **web panel**, **CLI**, **REST API**, **MCP server** and optional **LLM assistant** | One job model, five ways to drive it |

## What is in this manual

The manual is organised in twelve numbered chapters. Concepts come before
procedures, and every procedure links to the reference for the full set of
options. See [How to use this manual](how-to-use-this-manual.md) for
conventions, or jump straight to the [glossary](glossary.md) if a word is
unfamiliar.

> [!TIP]
> Press <kbd>/</kbd> anywhere in the manual to search. Inside the web panel
> the manual is also reachable from the command palette (<kbd>Ctrl</kbd>+<kbd>K</kbd>
> → *Manual*).
