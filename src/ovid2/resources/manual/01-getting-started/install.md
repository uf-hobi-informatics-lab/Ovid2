---
title: Install Ovid2
summary: Install the package with the extras you need, bootstrap the config tree, and verify the host.
audience: [user, developer, admin]
---

## 1. Install the package

Ovid2 is a standard Python package. Choose one method:

```bash
# Developer checkout (recommended while Ovid2 is pre-1.0)
git clone https://github.com/uf-hobi-informatics-lab/Ovid2.git ovid2
cd ovid2
uv sync --extra dev --extra spark --extra web --extra mcp
# or: python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev,spark,web,mcp]"

# Operator install as an isolated tool
pipx install ".[web,mcp]"        # or: uv tool install ".[web,mcp]"
```

This installs two console scripts: `ovid2` (every command) and
`ovid2-watch` (a shortcut for `ovid2 watch --follow-latest`).

### Optional extras

Install only the extras you need:

| Extra | Adds | Install when |
| --- | --- | --- |
| `spark` | PySpark 4.1 | you run Spark on the host (`--local`, tests) |
| `web` | FastAPI, uvicorn, Jinja, auth libraries | you serve the control panel |
| `mcp` | MCP SDK | you run Agent Ovid |
| `azure` | Azure Blob SDK | you use `azure_blob` connections |
| `s3` | boto3 | you browse or sample S3 from the panel |
| `gcs`, `postgres`, `mssql`, `databricks` | the matching SDKs | you browse those stores from the panel |
| `access` | JPype + jaydebeapi | you list Access tables on the host |
| `export` | reportlab, python-docx | you export tables to PDF or Word |
| `dev` | ruff, mypy, pytest, pre-commit | you work on Ovid2 |

> [!NOTE]
> Snowflake, Snowpark and DuckDB support is in the base install. Jobs run
> inside the Docker image, which bundles everything they need. The extras
> only matter for code that runs on the host.

## 2. Bootstrap the configuration tree

Run once per user account:

```bash
ovid2 init                 # writes sample configs and a secret template
ovid2 init --interactive   # also prompts for Snowflake credentials
```

```text
$XDG_CONFIG_HOME/ovid2/          (~/.config/ovid2)
├── configs/                     addresses
├── connections/                 storage accounts
├── engines/                     compute engines
├── llms/                        assistant model connections
├── pipelines/                   saved pipeline configs (web panel)
├── settings.json                panel / worker settings
└── secrets.d/                   one JSON per secret, mode 0600
$XDG_STATE_HOME/ovid2/           scheduler.db (jobs, audit, users), archive
$XDG_RUNTIME_DIR/ovid2/          event logs, per-run temp dirs
```

Full details: [Configuration](../08-operations/configuration.md).

## 3. Check the host

```bash
ovid2 doctor
```

`doctor` runs read-only checks and exits non-zero if any check fails with
severity `error`.

| Check | Passes when |
| --- | --- |
| `python` | the interpreter is 3.11+ |
| `import:*` | runtime dependencies import |
| `dir:*` | XDG directories exist and are writable |
| `configs` | address configs are present |
| `secret:*` | every `secrets.d/*.json` is mode 0600 |
| `docker` | the Docker CLI is on `PATH` and the daemon answers |
| `image` | the configured image (`$OVID2_DOCKER_IMAGE`, default `ovid2:latest`) exists locally |
| `systemd` | the service units are registered (servers only) |

Warnings (for example an optional dependency that isn't installed) are
informational.

## 4. Build the image (containerised runs)

```bash
ovid2 image build --tag ovid2:latest
```

The image contains Python 3.14, OpenJDK 21, Spark 4.1, the storage
connector jars and an Ovid2 wheel built from your checkout.

> [!WARNING]
> The source is **baked into** the image, not mounted. Rebuild after you
> change any code that runs inside a job — pipelines, formatters, mappers,
> fixers, drivers or the engine. See [The Docker image](../08-operations/docker-image.md).

## Next steps

- [First run — local](first-run-local.md) (no Docker)
- [First run — Docker](first-run-docker.md)
- [First run — web panel](first-run-web.md)
