---
title: Validate & test
summary: Catch spec errors before a run, test a pipeline on sample data, and add health-check cycles.
audience: [developer]
---

## Validate specs

```bash
ovid2 pipeline validate --pipeline myhospital   # load every formatter, mapper and fixer
ovid2 pipeline list                             # every discoverable package
ovid2 pipeline tables myhospital                # the canonical table list
```

`validate` checks that:

- every formatter and mapper parses against its JSON Schema;
- every adapter and op name exists;
- every fixer name is registered and in scope for the CDM;
- `output_columns_from` matches the CDM's fields.

In the web panel, **Validate** on a package page runs the same checks.

## Test on a sample

1. Cut a small sample of each source file (a few hundred patients).
2. Run `format` only, to a scratch folder, on DuckDB:

   ```bash
   ovid2 etl --local --engine duckdb --pipeline myhospital \
     --input_address sample_in --output_address scratch_out \
     --batch_id sample/2026_03 --transformations format --overwrite
   ```

3. Inspect the output in the Data Space: columns, row counts, a sample.
4. Add `map` and `fix`, then run a [health check](../04-running-jobs/health-checks.md)
   on the output.

## Compare with a previous delivery

`ovid2 migrate compare` diffs two output directories table by table, for
golden-file comparisons when you port or refactor a pipeline:

```bash
ovid2 migrate compare --help
```

## Add a report cycle

Health-check cycles are YAML manifests, so adding next quarter's cycle
needs no code change:

1. Put any brand-new check specs under `src/ovid2/resources/health/checks/`.
   Each must validate against `health_check.v1.json`.
2. Create `src/ovid2/resources/health/packages/<cycle>.yaml`:

   ```yaml
   id: 20r2
   label: "2020 R2"
   released: "2026-12-01"      # cycles sort newest first; the newest is the default
   description: "…"
   checks:
     - "1.01"
     - "1.02"
     - health.freshness
   ```

3. `ovid2 health --list-packages` now shows it, and the Submit page offers
   it.

The contract test `test_health_packages_valid.py` checks that every
manifest is well formed and that every check id resolves.

## Remember the image

Production jobs run the copy of your package **inside the Docker image**.
After changing a package:

```bash
ovid2 image build --tag ovid2:latest
```
