# Pipelines — kinds, packages, and the "new pipeline" form

A **pipeline** (`kind`) selects what a run *does*. It is configured as a
small JSON document (`$XDG_CONFIG_HOME/ovid2/pipelines/<name>.json`) and,
for some kinds, is backed by a **pipeline package** under the
`ovid2_pipelines.<name>` PEP-420 namespace
(`pipelines/ovid2_pipelines/<name>/`) that holds the formatters, mappers,
dictionaries, and fixer chains.

## Package-backed vs. package-free kinds

Not every kind needs a pipeline package. This matters for the "new
pipeline" form (`GET /pipelines/new` → `POST /pipelines`): for a
**package-backed** kind the matching package must exist (ETL scaffolds
one for you); for a **package-free** kind no package is created or
required — the `pipeline` field is just a name/label.

| Kind | Needs a package? | How the package is obtained | What `pipeline` is used for |
| --- | --- | --- | --- |
| `etl` | **Yes** | **Scaffolded** from the chosen template on submit (the package is named after the pipeline; its identity fields — DB name, HARVEST datamart id, DATAVANT key — are reset to placeholders). | Selects the formatter/mapper/fixer package the worker runs. |
| `analysis` | **Yes** | **Pick an existing installed package** on the form (it is *not* scaffolded). | Runs the `format` stage → resolves per-table formatters + dictionaries from the chosen package. `name` is just the config label. |
| `dq_checks` | **No** | — (package-free) | Runs the PCORnet EDC / Data-Characterization checks for the **report cycle** you pick (`20r1`, …). Reads CDM tables directly (reuses the `health` engine); `pipeline == name` is only a label. |
| `health` | **No** | — (package-free) | Address path/schema scoping only. Reads CDM tables directly and runs data-characterization check specs from `resources/health/checks/`. Also does a *tolerant* CDM lookup that soft-fails when no package exists. |
| `copy` | **No** | — (package-free) | Address path/schema scoping only. Streams table-to-table through the driver/stager; bypasses the format→map→fix chain. |
| `move` | **No** | — (package-free) | Same as `copy` (driver-native successor). |

### The "new pipeline" form per kind

- **`etl`** — shows the *ETL template* picker; on submit a package is
  scaffolded under the pipeline name and `pipeline == name`.
- **`analysis`** — shows the *Pipeline package* picker (populated from the
  installed `ovid2_pipelines.*` packages). `pipeline` is the chosen
  package; `name` is the config label, so several configs can target one
  package (e.g. `analysis_acme` → `acme`).
- **`dq_checks`** — shows the *EDC report cycle* picker (populated from the
  registered cycle packages under `resources/health/packages/`). The
  chosen cycle(s) are the pipeline's *type* and are persisted on the
  config (field `packages`). No installed package and no CDM field are
  required — the CDM is resolved from the pipeline default at run time.
- **`health` / `copy` / `move`** — show neither package picker; they are
  package-free and `pipeline == name` is only a label for path scoping.


The authoritative set of package-free kinds is
`ovid2.web.models.PACKAGE_FREE_KINDS`
(`{"health", "dq_checks", "copy", "move"}`); the full list of kinds is
`ovid2.web.models.PIPELINE_KINDS`.

## Why this matters (a note for operators)

Because `health` / `dq_checks` / `copy` / `move` are **package-free**,
creating one for a brand-new name (e.g. an EDC/health-report pipeline
named `EDC_repot`) does **not** scaffold anything and does **not** require
a matching `ovid2_pipelines.<name>` package. Earlier builds incorrectly
demanded an installed package for *every* kind and rejected these with:

> Pipeline '<name>' is not installed under ovid2_pipelines.

That gate now applies only to the package-backed kinds
(`etl` / `analysis`). The `pipeline` field is still required
to be non-empty for every kind because it scopes the address path.

Enforced in two places, both keyed off `PACKAGE_FREE_KINDS`:

- `pipelines_svc._validate_write` — create/update time.
- `jobs_svc._validate_submit` — submit/run time.

## Open pipelines: choosing storage at run time

A pipeline may keep its input and/or output side **open** — unbound at
create time (the "Keep open — choose at run time" radio on the create
form; stored as an empty `input_address` / `output_address`). The open
side is bound per-run.

On the **Pipelines page** (`GET /pipelines`), an open row's *Actions*
cell renders an inline storage-account `<select>` for **each** open side
(`input_connection` / `output_connection`, populated from the saved
storage accounts) plus a `batch_id` field, and a **run** button:

- Pick the account(s) for the open side(s), optionally type a
  `batch_id` (which doubles as the schema to read from / write to), and
  click **run**. `POST /pipelines/{name}/run` resolves each chosen
  account into an address (at the account root) and submits the job
  immediately — you land on `/runs/{id}`, no detour.
- Leave an open side's account unpicked and the run **falls back** to
  the Submit form (pre-selected on this pipeline), where you can *browse*
  the account to a specific sub-folder. The row also carries a
  `browse…` link straight to that form.

A bound (non-open) side is never asked for at run time. Note that an
open **input** cannot use the `watch` trigger — `watch` enumerates new
batch folders under a bound input.

## Health checks: report-cycle packages (`20r1`, `20r2`, …)

The `health` and `dq_checks` kinds run the PCORnet EDC / Data-
Characterization checks plus the Ovid2-native health panels. Each
individual check is a declarative YAML spec under
`src/ovid2/resources/health/checks/` (operator + params + thresholds).
Do **not** confuse these with the partner *pipeline packages* (`acme`,
`site`, …) used by `analysis`; those are unrelated. `dq_checks` selects
its checks by report **cycle** (its pipeline "type"); `health` can select
by cycle, part, or explicit check id.

Because the EDC program ships a new report **cycle** every quarter — and
successive cycles *share* many checks while adding or changing a few — a
cycle is modelled as its own manifest:

```
src/ovid2/resources/health/packages/<cycle>.yaml
```

```yaml
id: 20r1
label: "2020 R1 (baseline)"
released: "2026-09-11"        # cycles are ordered by this date, newest first
description: "…"
checks:                        # ids matching a spec's `id` or `name`
  - "1.01"
  - "1.02"
  - health.freshness
  # …
```

A check id may appear in **several** package manifests — that is how
packages share checks. Selecting a package runs the *union* of its
member checks.

### Adding next quarter's cycle (`20r2`) — no code change

1. Drop any brand-new check specs under `resources/health/checks/`
   (each validated against `health_check.v1.json`).
2. Create `resources/health/packages/20r2.yaml` listing the new checks
   **plus** the `20r1` checks that carry forward.

The submit form and `ovid2 health --list-packages` pick it up
automatically, and because `20r2.released` is later it becomes the
default cycle in the picker.

### Selecting checks at run time

Precedence is **`checks` > `packages` > `parts`** (a higher one silences
the lower). All three are additive/back-compatible:

| Surface | Package selection |
| --- | --- |
| Web submit form | *Report cycle (package)* dropdown shown for `kind=health`, defaulting to the newest cycle. |
| CLI | `ovid2 health --packages 20r1` (and `ovid2 health --list-packages`). |
| REST | `POST /api/v1/runs` with `kind:"health"`, `"packages":["20r1"]`; catalogue at `GET /api/v1/health/packages`. |
| MCP | `submit_health(packages=["20r1"])`; discover via the `list_health_packages` tool. |

Enforced by `tests/contract/test_health_packages_valid.py` (every
manifest validates, every referenced check id resolves, packages ordered
most-recent first).

## Running EDC checks against *any* PCORnet database (no duplicate pipelines)

The EDC / data-characterization checks are identical for every PCORnet
partner, so there is **no** reason to author one health pipeline per
database. Instead, use the generic package-free label **`pcornet_edc`**
and select the storage account, database, and schema at run time.

- **The label carries the CDM, not a database.** `pcornet_edc` is a
  well-known entry in `ovid2.cdm.registry.GENERIC_CDM_LABELS`
  (`pcornet_edc → pcornet_7_0`). `resolve_cdm_for_pipeline` recognises it
  first and pins the CDM directly — it never imports an
  `ovid2_pipelines.<name>` package and never reads a partner's
  `DEFAULT_PIPELINE_DB_NAME`. The `health` / `dq_checks` CLIs and
  `submit_health` default `pipeline` to `pcornet_edc`.
- **The database/schema come from the run-time selection.** Pick a
  *storage account* (connection), then browse its databases and schemas
  (dynamically enumerated for Snowflake/disc/s3 via the driver's
  `list_dir`). The selection resolves-or-creates an address on the fly;
  nothing is baked into a package or a pre-saved address.
- **A warehouse has no runnable root — choose a database/schema.** For
  warehouse connections (`snowflake` / `postgres` / `mssql` /
  `databricks`), selecting only the *account* is **not** a complete
  choice: a database (and usually a schema) is mandatory. The run modal
  will not let you submit at warehouse root, and the server rejects an
  empty-location warehouse address with *"Choose a database/schema for
  '<account>' before submitting — a warehouse connection has no runnable
  root."* (Filesystem connections keep their valid root.) If you skip
  this, every table read fails with *"No Snowflake database specified"*
  and the health run reports an error instead of silently passing.

| Surface | How you select storage → database → schema |
| --- | --- |
| Web submit form | *Pipeline* dropdown → pick `pcornet_edc` (under "Reusable EDC / health labels"). Input/Output storage cards → **Browse storage** → choose account, then click into the database and schema. |
| CLI | `ovid2 health --pipeline pcornet_edc --input_connection <account> --input_location "DATABASE/SCHEMA" --output_connection <account> --output_location "DATABASE/SCHEMA" --batch_id 20260101 --packages 20r1` (a saved `--input_address` / `--output_address` still works; `--sf_database` / `--sf_schema` remain as overrides). |
| REST | `POST /api/v1/runs` with `kind:"health"`, `pipeline:"pcornet_edc"`, and `input_connection`/`input_location` (+ `output_connection`/`output_location`) instead of `input_address`/`output_address`. |
| MCP | `submit_health(input_connection=<account>, input_location="DATABASE/SCHEMA", output_connection=<account>, output_location="DATABASE/SCHEMA", packages=["20r1"])`; discover accounts via `list_connections` and their databases/schemas via `browse_storage`. |

The results table (`dcq_results`) and the JSON report are written to the
selected **output** storage account. Verified end-to-end by
`tests/unit/web/test_edc_runtime_selection_http.py`; the decoupling
guarantees are pinned by `tests/unit/cdm/test_generic_edc_label.py` and
`tests/unit/config/test_runtime_address_selection.py`.

## `copy` / `move` tables: explicit lists, and `all` for self-describing sources

`copy` and `move` bypass the format→map→fix chain and stream named tables
from a source address to a destination address. Their `--tables` list is
normally **explicit** — `ovid2 tools copy|move` use `nargs='+'` and have no
built-in `all` sentinel — so the Run modal shows a required tables field and
the web tier rejects an empty/`all` list with *"copy / move need an explicit
list of tables"*.

**Exception — sources that can enumerate their own tables.** A Microsoft
Access `.accdb` / `.mdb` file is a self-describing container: one file holds
many user tables. For such a source you may pass `all` (or leave the Run
modal's tables field blank); the executor enumerates every user table in the
file at run time and copies each as a separate destination table. Which
sources support this is decided by the driver, not hardcoded:

- `Driver.supports_expand_all()` — a static, secret-free capability flag the
  web tier consults to decide whether to *accept* `all` for a source.
- `Driver.expand_all_tables(location)` — the executor-side expansion, which
  honours the address's optional `tables` allow-list / `exclude_tables`
  deny-list.

The `access` driver overrides both. `all` for an Access source therefore
means "every user table in the file, minus `exclude_tables`". Example:

```
ovid2 tools copy --from drugdb_accdb --to warehouse_sandbox \
    --pipeline drugdb --batch_id 20260915 --tables all
```

### Browsing Access tables in the web tier

An `access` **connection** points at the *directory* holding the file(s); an
`access` **address (location)** selects one specific file via `path`
(relative to the connection's `host_path`). The storage browser lets you
descend directory → `.accdb` file → its tables. Table enumeration needs a
JVM: in the ETL container it uses the Spark JVM; in the web tier it uses a
lightweight JVM via the optional `access` extra
(`pip install 'ovid2[access]'` → `JPype1` + `jaydebeapi`) and the UCanAccess
jars. Point the web host at the jars with `$OVID2_UCANACCESS_JARS` (a
directory or `os.pathsep`-separated list); it otherwise falls back to
`$SPARK_HOME/jars`. When neither the extra nor the jars are present, the
browser gracefully stops at the file level (you can still copy the whole file
with `--tables all`, which enumerates inside the container).

