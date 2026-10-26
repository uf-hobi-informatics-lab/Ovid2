# Docker-less local quick-start (`ovid2 etl --local`)

This walkthrough runs a complete Ovid2 ETL pipeline — the
`format → deduplicate → map → fix → write` chain — **on your laptop with no
Docker, no Spark cluster, and no cloud connectors**. It reads a CSV from a
local directory, deduplicates it against a tiny custom data model, and writes
the result back to another local directory.

It is the honest minimum: the pipeline itself is Docker-agnostic, and the
`--local` flag routes `ovid2 etl` straight to the in-process runner instead of
shelling out to `docker run` / `docker compose`.

> **Not zero-dependency.** The default `pyspark` engine builds a local
> `SparkSession` (`local[*]`), so you still need the `[spark]` extra **and a
> JDK** (Java 11 or 17). There is no pure-Python compute engine. What `--local`
> removes is Docker, the container image, the multi-worker cluster, and any
> remote service.

---

## 1. Prerequisites

```bash
# A JDK (Java 11 or 17). On Debian/Ubuntu:
sudo apt-get install -y openjdk-17-jre-headless
java -version        # confirm it resolves

# Ovid2 + local Spark, in a virtualenv
python -m venv .venv && . .venv/bin/activate
pip install -e ".[spark]"
```

## 2. Lay out a tiny pipeline

A pipeline is a package under the `ovid2_pipelines` namespace. Create a working
directory and a `demo` pipeline inside it:

```bash
export QS="$PWD/ovid2-local-demo"
mkdir -p "$QS/pipelines/ovid2_pipelines/demo/formatters"
mkdir -p "$QS/configs"
mkdir -p "$QS/data/input/demo/20260101"
mkdir -p "$QS/data/output"
```

### 2a. Pipeline package

`$QS/pipelines/ovid2_pipelines/demo/__init__.py`:

```python
"""Demo pipeline package for the Docker-less --local quick-start."""
```

`$QS/pipelines/ovid2_pipelines/demo/pipeline_settings.py` — binds the pipeline
to a data model and registers a tiny in-code CDM at import time (no changes to
the installed `ovid2` package required):

```python
"""Minimal demo pipeline settings + in-code CDM registration."""

from __future__ import annotations

from ovid2.cdm.handle import CdmHandle
from ovid2.cdm.registry import register

cdm = "demo_cdm"

_DEMO_CDM = CdmHandle(
    name="demo_cdm",
    version="1.0",
    composite_keys={"person": ("PERSON_ID",)},  # dedup key for the person table
    reference_tables=frozenset(),
    id_columns={},
    flavour_of_null=frozenset({"", None}),
    shared_views=(),
)

try:
    register(_DEMO_CDM)
except ValueError:
    pass  # already registered (idempotent on re-import)
```

### 2b. Formatter spec

`$QS/pipelines/ovid2_pipelines/demo/formatters/__init__.py`:

```python
"""Formatter specs for the demo pipeline."""
```

`$QS/pipelines/ovid2_pipelines/demo/formatters/person.yaml` — a
`source_passthrough` adapter that reads `*person*.csv` and projects two columns
through. Omitting `output_columns_from` skips CDM-field validation, so no field
YAMLs are needed:

```yaml
v: 1
formatter: person
adapter: source_passthrough
input_file_name: "*person*.csv"
sources:
  person:
    glob: "*person*.csv"
    lowercase_columns: true
projections:
  PERSON_ID: person_id
  NAME: name
```

> No `fixer_configs.py` is created, so the `fix` stage is a no-op. No mapper
> spec is created, so the `map` stage is a no-op.

## 3. Address configs (local disc → local disc)

`$QS/configs/demo_input.json`:

```json
{
  "src_name": "disc",
  "src_args": {
    "path": "REPLACE_WITH_$QS/data/input/demo/20260101",
    "format": "csv",
    "lowercase_columns": true,
    "single_file": true
  }
}
```

`$QS/configs/demo_output.json`:

```json
{
  "src_name": "disc",
  "src_args": {
    "path": "REPLACE_WITH_$QS/data/output/demo",
    "format": "csv",
    "single_file": true
  }
}
```

Substitute the real absolute paths (the shell won't expand `$QS` inside a JSON
file). **An address is the exact location of its data**: the disc driver
reads `{path}/{glob}` and writes `{path}/{table}.csv` — `pipeline` and
`batch_id` are never appended. Point the input address at the folder that
directly holds the files. Writing into an output that already holds tables
is refused unless you pass `--overwrite`.

## 4. Sample input data

`$QS/data/input/demo/20260101/person.csv` — note the duplicate row:

```csv
person_id,name
1,Alice
2,Bob
2,Bob
3,Carol
```

## 5. Run it — no Docker

```bash
export PYTHONPATH="$QS/pipelines"        # put the demo pipeline on sys.path
export OVID2_CONFIGS_DIR="$QS/configs"   # where demo_input/demo_output live

ovid2 etl --local \
  --pipeline demo \
  --batch_id demo/20260101 \
  --tables person \
  --transformations format deduplicate \
  --input_address demo_input \
  --output_address demo_output \
  --engine pyspark
```

Equivalent without the flag: `OVID2_EXECUTION_MODE=inprocess ovid2 etl …`.

Expected tail:

```
[ovid2] ETL run
[ovid2]   pipeline=demo batch_id=demo/20260101 tables=['person']
[ovid2]   transformations=['format', 'deduplicate']
[ovid2]   engine=pyspark instance_id=...
[ovid2] Complete. failed=0 succeeded=1
```

## 6. Check the output

```bash
cat "$QS"/data/output/demo/person.csv
```

```
PERSON_ID	NAME
1	Alice
2	Bob
3	Carol
```

Four input rows became three — the duplicate `Bob` was removed by the
`deduplicate` stage using the `PERSON_ID` composite key from `demo_cdm`. No
container was started at any point.

---

## How this maps to a real deployment

| Quick-start (local) | Production |
| --- | --- |
| `ovid2 etl --local` (in-process) | `ovid2 etl` (Docker host mode → isolated Spark cluster) |
| In-code `CdmHandle` in `pipeline_settings.py` | YAML CDM tree under `src/ovid2/resources/cdms/<cdm>/` + a registration module |
| Local disc → disc | Any of the 8 drivers (Snowflake, S3, Azure Blob, GCS, …) |
| `local[*]` Spark | Multi-worker cluster or Snowpark |

The `--local` path exercises the **same** `run_pipeline` code as container mode;
it only bypasses the host-side Docker dispatch. See
[`docs/formatter-spec.md`](./formatter-spec.md) and
[`pipelines/README.md`](../pipelines/README.md) for authoring real pipelines
and [`README.md`](../README.md) for the full Docker-based workflow.
