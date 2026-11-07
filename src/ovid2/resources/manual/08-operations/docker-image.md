---
title: The Docker image
summary: What the job image contains, when you must rebuild it, and how tags are chosen.
audience: [admin, developer]
---

Jobs run inside the **Ovid2 image** (default tag `ovid2:latest`; override
with `$OVID2_DOCKER_IMAGE`). It contains:

- Python 3.14 on `python:3.14-slim`, and OpenJDK 21;
- Apache Spark 4.1 (Scala 2.13);
- storage connector jars: Snowflake, Hadoop-AWS, Azure, GCS, and
  UCanAccess with a custom JDBC dialect for Access;
- an **Ovid2 wheel built from your checkout**, plus the pipeline packages;
- `entrypoint.sh`, and an `nss-wrapper` overlay so containers run as your
  uid.

## Build

```bash
ovid2 image build --tag ovid2:latest
# or
docker build -f docker/Dockerfile -t "${OVID2_DOCKER_IMAGE:-ovid2:latest}" .
```

You can also build from the Setup wizard (`/install`), which streams the
build log in the browser.

## When to rebuild

> [!WARNING]
> The source is **copied into the image at build time**. It isn't
> mounted. Edits on the host don't reach jobs until you rebuild.

| You changed | Rebuild? |
| --- | --- |
| pipeline packages (formatters, mappers, dictionaries, fixer configs) | **yes** |
| `src/ovid2/core/`, `transformations/`, `formatters/`, `mappers/`, `transforms/` | **yes** |
| driver `spark.py` / `duckdb.py`, engines | **yes** |
| `docker/` (Dockerfile, entrypoint, jars) | **yes** |
| the web panel, its templates or static assets | no — restart `ovid2 web` |
| connection, address, engine or pipeline JSON configs, `settings.json` | no |
| the manual | no — the panel serves it from the installed package |

## Tags and dev instances

`ovid2 dev` instances use their own tag, `ovid2:dev-<user>`, so a
developer's build never replaces the shared image. See
[Dev instances](../01-getting-started/dev-instances.md).
