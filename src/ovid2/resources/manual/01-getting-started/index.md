---
title: Getting started
summary: From nothing to a first successful run — choose the path that matches your environment.
audience: [user, developer, admin]
---

This chapter takes you from an empty machine to a finished run. Pick the
route that fits your environment; you can do the others later.

<div class="cards">
  <a class="card" href="install.md"><strong>1 · Install</strong><span>Install the package, create the config tree, and check the host with <code>ovid2 doctor</code>.</span></a>
  <a class="card" href="first-run-local.md"><strong>2a · Local run (no Docker)</strong><span>Run a tiny pipeline in-process on your laptop. Takes about five minutes.</span></a>
  <a class="card" href="first-run-docker.md"><strong>2b · Docker run</strong><span>Build the image and run the bundled example pipeline on an isolated Spark cluster.</span></a>
  <a class="card" href="first-run-web.md"><strong>2c · Web panel run</strong><span>Start the panel and worker, connect storage, and submit from the browser.</span></a>
  <a class="card" href="dev-instances.md"><strong>Shared server?</strong><span>Give every developer a private, isolated panel with <code>ovid2 dev</code>.</span></a>
</div>

## Requirements at a glance

| Need | For | Notes |
| --- | --- | --- |
| Python 3.11+ | everything | 3.14 is the development and image target. 3.15 is not supported yet. |
| JDK 17 or 21 | Spark runs on the host (`--local`) | Not needed for DuckDB or in-container runs. |
| Docker + Compose | containerised runs, isolated clusters | Not needed for `--local`. |
| Storage credentials | the systems you read and write | Stored in `secrets.d/`, mode 0600. |
| OMOP `CONCEPT` bundle | OMOP pipelines, concept joins | Under `$OVID2_MATERIAL_DIR/omop_cdm/v5.3/`. |

> [!TIP]
> If you only want to *see* Ovid2 work, take the [local run](first-run-local.md).
> It needs no Docker, no cluster and no cloud account.

## What you will have at the end

- The `ovid2` command on your `PATH`.
- A config tree at `$XDG_CONFIG_HOME/ovid2/`, holding connections,
  addresses, engines, pipeline configs, `settings.json` and `secrets.d/`.
- A finished run with an instance id, an event log and output at the exact
  address you chose.

Next: [Install Ovid2](install.md).
