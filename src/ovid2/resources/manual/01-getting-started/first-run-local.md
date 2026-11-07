---
title: First run — local, no Docker
summary: Run a complete format → deduplicate pipeline in-process on your laptop, disc to disc, in about five minutes.
audience: [user, developer]
---

This walkthrough runs a real Ovid2 pipeline **on your machine with no
Docker, no cluster and no cloud account**. It reads a CSV from one folder,
removes a duplicate row using a tiny custom data model, and writes the
result to another folder.

> [!NOTE]
> `--local` removes Docker, the image and the cluster, but the default
> `pyspark` engine still starts a local Spark session. You need the
> `[spark]` extra and a JDK (17 or 21). If you would rather avoid Java,
> add `--engine duckdb`.

## 1. Prerequisites

```bash
java -version                      # JDK 17 or 21 (skip for --engine duckdb)
pip install -e ".[spark]"          # from your Ovid2 checkout
```

## 2. Lay out a tiny pipeline

A pipeline is a Python package in the `ovid2_pipelines` namespace. Create a
working directory with a `demo` pipeline:

```bash
export QS="$PWD/ovid2-local-demo"
mkdir -p "$QS/pipelines/ovid2_pipelines/demo/formatters" \
         "$QS/configs" "$QS/data/input/demo/20260101" "$QS/data/output"
touch "$QS/pipelines/ovid2_pipelines/demo/__init__.py" \
      "$QS/pipelines/ovid2_pipelines/demo/formatters/__init__.py"
```

### Settings: bind the pipeline to a data model

`$QS/pipelines/ovid2_pipelines/demo/pipeline_settings.py` registers a
one-table CDM in code, so you don't have to change the installed package:

```python
"""Minimal demo pipeline settings + in-code CDM registration."""

from __future__ import annotations

from ovid2.cdm.handle import CdmHandle
from ovid2.cdm.registry import register

cdm = "demo_cdm"

_DEMO_CDM = CdmHandle(
    name="demo_cdm",
    version="1.0",
    composite_keys={"person": ("PERSON_ID",)},  # the dedup key
    reference_tables=frozenset(),
    id_columns={},
    flavour_of_null=frozenset({"", None}),
    shared_views=(),
)

try:
    register(_DEMO_CDM)
except ValueError:
    pass  # already registered
```

### Formatter: describe the `person` table

`$QS/pipelines/ovid2_pipelines/demo/formatters/person.yaml`:

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

This pipeline has no mapper spec and no fixer chain, so the map and fix
stages do nothing.

## 3. Point at the input and output

Addresses are small JSON files. Replace `/ABS/PATH` with the real value of
`$QS`, because JSON files don't expand shell variables.

`$QS/configs/demo_input.json`:

```json
{
  "src_name": "disc",
  "src_args": { "path": "/ABS/PATH/data/input/demo/20260101", "format": "csv",
                "lowercase_columns": true, "single_file": true }
}
```

`$QS/configs/demo_output.json`:

```json
{
  "src_name": "disc",
  "src_args": { "path": "/ABS/PATH/data/output/demo", "format": "csv", "single_file": true }
}
```

> [!IMPORTANT]
> **An address is the exact location of its data.** The input address
> points at the folder that holds the files; the output address is the
> folder the tables are written to. Ovid2 never appends a pipeline name or
> a batch id to either path.

## 4. Add sample data

`$QS/data/input/demo/20260101/person.csv`. Note the duplicate `Bob`:

```text
person_id,name
1,Alice
2,Bob
2,Bob
3,Carol
```

## 5. Run it

```bash
export PYTHONPATH="$QS/pipelines"       # put the demo pipeline on sys.path
export OVID2_CONFIGS_DIR="$QS/configs"  # where the two addresses live

ovid2 etl --local \
  --pipeline demo --batch_id demo/20260101 \
  --tables person --transformations format deduplicate \
  --input_address demo_input --output_address demo_output
```

The output ends with:

```text
[ovid2] ETL run
[ovid2]   pipeline=demo batch_id=demo/20260101 tables=['person']
[ovid2]   transformations=['format', 'deduplicate']
[ovid2]   engine=pyspark instance_id=20260101-120000-a1b2c3
[ovid2] Complete. failed=0 succeeded=1
```

## 6. Check the result

```bash
cat "$QS"/data/output/demo/person.csv
```

```text
PERSON_ID	NAME
1	Alice
2	Bob
3	Carol
```

Four rows went in and three came out. The `deduplicate` stage removed the
duplicate using the `PERSON_ID` key from `demo_cdm`.

> [!TIP]
> Run it a second time and Ovid2 refuses, because the output already holds
> a table. Add `--overwrite` to replace it. See
> [Overwrite & batches](../04-running-jobs/overwrite-and-batches.md).

## From laptop to production

| This walkthrough | A real deployment |
| --- | --- |
| `ovid2 etl --local` (in-process) | `ovid2 etl` (an isolated Docker Spark cluster per run) |
| In-code `CdmHandle` | YAML CDM under `src/ovid2/resources/cdms/` |
| Disc → disc | Any [driver](../10-reference/drivers-engines.md) |
| `local[*]` Spark | A multi-worker cluster, Snowpark, Databricks or DuckDB |

`--local` runs exactly the same pipeline code as container mode. It only
skips the Docker dispatch step.

Next: [First run — Docker](first-run-docker.md) or
[Authoring pipelines](../06-authoring/index.md).
