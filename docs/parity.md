# Legacy-Ovid parity runs

Ovid2 pipelines that declare `ETL_PROFILE = "legacy_ovid"` must produce the same output
as legacy Ovid. The design is in the adr-legacy-parity-port design note;
current status is in `docs/legacy-parity-plan.md`.

## 1. Sample a batch

```bash
python -m ovid2.tools.parity_sample --src <inbox>/<P>/<batch> \
    --dst <sandbox>/input/<P>/<batch>s --patients 1500 \
    [--max-scan-gb 2] [--skip-suffix .gpg .zip .gz .tar]
```

The tool picks N patients from the patient file. It keeps only those patients' rows in
every file that has a patient column, and copies small lookup files whole.
`--max-scan-gb` bounds how much of each file is filtered, for multi-TB OMOP feeds.
Both engines read the same sample, so the bound does not affect parity.

## 2. Run legacy and Ovid2, then diff

```bash
scripts/legacy_port/parity_run.sh <partner> <sandbox>/input/<P>/<batch>s
```

The script runs legacy Ovid in one container of the legacy image (`local[N]`). It then
runs `ovid2 etl --local` once per stage prefix (`format` / `+deduplicate` / `+map` / all)
and diffs each stage with `ovid2.tools.golden_diff`. Reports land in
`<sandbox>/report/<partner>/<batch>/<stage>.{txt,json}`.

| Env | Meaning |
|---|---|
| `PARITY_SKIP_LEGACY=1` | reuse the previous legacy run |
| `PARITY_ONLY="fix"` | re-run / diff only the listed stages |
| `PARITY_MASK_EXTRA="COL …"` | extra masked columns (overrides the per-partner default) |
| `PARITY_SANDBOX`, `LEGACY_ROOT`, `LEGACY_IMAGE`, `PARITY_CORES` | locations / sizing |

Always masked: `UPDATED`, `GEOCODEID`, `ASSIGNMENT_DATE` (wall clock / partitioning), and the
harvest `REFRESH_*_DATE` columns, which hold the run date (`--mask` accepts fnmatch globs).
Default extra masks: theta `PROCEDURESID`, gamma `ADDRESSID`. Legacy derives both from
`monotonically_increasing_id()`, so two legacy runs disagree on them too.

## 3. Read a diff

`[MISS]` means legacy produced a file and Ovid2 did not. `[EXTRA]` is the reverse.
`[DIFF]` lists row counts and sample rows from each side. Fix differences in the
framework (`src/ovid2/legacy/*`, the migrators, `gen_reference.py`) and then regenerate.
**Never hand-edit a generated port.**

## Known failures that are identical in legacy

- tumor_site batches with `out_TR_*` file names: the `*SITE_TR_*` glob matches nothing.
- demo `procedures`: the custom fix script is missing, so legacy writes `FAILED_fixed_procedures.csv`.
- beta / chi2 `obs_gen`: the legacy formatter is empty, so neither side produces output.
- mu: `loinc_mover_fix` leaves an empty `fixed_lab_result_cm.csv` when no input table exists.
- Partner formatters that reference columns absent from a given batch fail on both sides
  (for example, kappa 20261014 `diagnosis`).
