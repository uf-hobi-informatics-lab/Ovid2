---
title: Core concepts
summary: The mental model behind Ovid2 — read this once and every other page will make sense.
audience: [user, researcher, developer, admin]
---

Ovid2 has a small number of nouns, and they combine in only a few ways.
This chapter introduces them in dependency order.

<div class="cards">
  <a class="card" href="architecture.md"><strong>Architecture</strong><span>Storage, compute and algorithms as three independent axes, and the tiers that run them.</span></a>
  <a class="card" href="storage.md"><strong>Connections & addresses</strong><span>How Ovid2 reaches storage, and why an address is an exact location.</span></a>
  <a class="card" href="dataspace.md"><strong>The Data Space</strong><span>Every storage account as one browsable folder tree.</span></a>
  <a class="card" href="pipelines.md"><strong>Pipelines & kinds</strong><span>Pipeline packages, saved configs, and the seven job kinds.</span></a>
  <a class="card" href="etl-stages.md"><strong>The five ETL stages</strong><span>Format, deduplicate, map, fix, write: what each stage guarantees.</span></a>
  <a class="card" href="engines.md"><strong>Engines</strong><span>PySpark, Databricks, Snowpark, DuckDB, and how to choose.</span></a>
  <a class="card" href="cdms.md"><strong>Data models</strong><span>How a CDM is described as YAML, so the engine stays CDM-agnostic.</span></a>
  <a class="card" href="trust-model.md"><strong>Trust model</strong><span>Agent → API → executor, scoped secrets, and approval gates.</span></a>
  <a class="card" href="isolation-and-events.md"><strong>Isolation & events</strong><span>Instance ids, per-run clusters, and the event log behind every live view.</span></a>
</div>

## The whole model in one sentence

A **job** of some **kind** runs a **pipeline** on an **engine**, reading
from an input **address** and writing to an output **address**. Each
address is a location inside a **connection**, and the job shapes the data
into a **CDM**.

```text
            ┌──────────── pipeline (formatters · mappers · fixers) ───────────┐
input  ─►   │  format ─► deduplicate ─► map ─► fix ─► write                   │   ─► output
address     └──────────────────────── on an engine ───────────────────────────┘      address
(connection + location)                 shaped to a CDM                     (connection + location)
```
