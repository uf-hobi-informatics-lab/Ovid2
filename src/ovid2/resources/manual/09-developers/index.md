---
title: Developer guide
summary: Work on Ovid2 itself — set up, find your way around, respect the invariants, extend it, and contribute.
audience: [developer]
---

<div class="cards">
  <a class="card" href="repository.md"><strong>Repository tour</strong><span>Where every subsystem lives.</span></a>
  <a class="card" href="invariants.md"><strong>Invariants</strong><span>The rules, and the contract tests that enforce them.</span></a>
  <a class="card" href="extending.md"><strong>Extending Ovid2</strong><span>Add a driver, engine, CDM, vocabulary, fixer, op, adapter or tool.</span></a>
  <a class="card" href="portable-code.md"><strong>Portable engine code</strong><span>Write code once for Spark, Snowpark and DuckDB.</span></a>
  <a class="card" href="events.md"><strong>Events</strong><span>Emitting typed events and writing renderers.</span></a>
  <a class="card" href="testing.md"><strong>Testing</strong><span>Markers, fixtures, and the verify-before-done rule.</span></a>
  <a class="card" href="contributing.md"><strong>Contributing</strong><span>Style, change discipline, public mirror.</span></a>
  <a class="card" href="writing-docs.md"><strong>Writing documentation</strong><span>Add or edit pages in this manual.</span></a>
</div>

## Set up

```bash
git clone https://github.com/uf-hobi-informatics-lab/Ovid2.git ovid2 && cd ovid2
uv sync --extra dev --extra spark --extra web --extra mcp
uv run pre-commit install
uv run pytest -m "unit or contract"
```

Then start a private instance with `ovid2 dev up`. See
[Dev instances](../01-getting-started/dev-instances.md).

## The quality gates

```bash
scripts/ci_checks.sh                  # lint, typecheck, contract, unit: what CI runs
scripts/ci_checks.sh --docker contract unit   # the same, in a clean python:3.14 image
uv run ruff check --fix && uv run ruff format
uv run mypy src/ovid2                 # strict
```
