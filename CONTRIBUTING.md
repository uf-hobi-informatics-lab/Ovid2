# Contributing to Ovid2

Thanks for your interest in Ovid2. This document explains how to set up a
development environment, the quality bar every change must clear, and the
conventions that keep the codebase coherent.

> Ovid2 is a data-processing engine used with healthcare pipeline data. Read
> [`SECURITY.md`](SECURITY.md) before contributing — **never** put credentials,
> secrets, tokens, or real patient data in code, tests, issues, or PRs.

## Ground rules

The project's design principles, style rules, and change discipline are
summarised below (maintainers keep the full specification in `AGENTS.md` in
the upstream repository). The most important invariants:

1. **Lazy Spark end-to-end** — no `.collect()`, `.toPandas()`, `.take()`,
   `.count()`-for-branching, or intermediate CSV writes in `src/ovid2/core/`,
   `transformations/`, `formatters/`, `mappers/`, or `pipelines/`.
2. **Declarative first** — formatters, mappers, and fixer chains are YAML/dict
   specs. Hand-written classes are the exception, gated behind `adapter: custom`
   / `class:`.
3. **CDM/ETL independence** — the ETL engine never imports a specific CDM.
4. **Display isolation** — pipeline code emits events on the bus; it never
   imports `src/ovid2/display/`.
5. **No hardcoded credentials, hosts, or absolute paths** outside
   `resources/` / configs.

These are not suggestions: each is enforced by a contract test in
`tests/contract/` that runs on every commit.

## Development setup

```bash
git clone -b dev https://github.com/uf-hobi-informatics-lab/Ovid2.git ovid2 && cd ovid2
python -m venv .venv && . .venv/bin/activate

# Dev tooling + local Spark (needs a JDK 11/17 for the Spark test fixture):
pip install -e ".[dev,spark]"

# Install the pre-commit hooks (ruff, ruff-format, mypy, contract tests):
pre-commit install
pre-commit install --hook-type pre-push
```

Want to try it end-to-end without Docker? See
[`docs/quickstart-local.md`](docs/quickstart-local.md).

## The quality bar

A change is **not** done until all of the following pass. This mirrors CI
(`.github/workflows/ci.yml`) and the pre-commit config.

```bash
# 1. Lint + format
ruff check .
ruff format --check .

# 2. Types (strict)
mypy --config-file=pyproject.toml src/ovid2

# 3. Contract invariants (fast, no Spark/Docker required)
pytest -q -m contract

# 4. Unit tests (needs the [spark] extra + a JDK for Spark-backed tests)
pytest -q -m "not contract and not integration and not real_connection \
  and not real_cluster and not perf and not endurance"
```

Test tiers:

- **Contract** (`tests/contract/`) — source-tree invariants. Always run these.
- **Unit** (`tests/unit/`) — single class/function; inline DataFrame builders in
  `tests/helpers/inline_builders.py`.
- **Integration** (`tests/integration/`) — multi-component; needs the `spark`
  fixture. Run with `pytest -m integration`.
- **`real_connection` / `real_cluster` / `perf` / `endurance`** — opt-in only;
  never in the default CI suite.

Changes to `src/ovid2/core/`, `transformations/`, or
`formatters/` **require** a corresponding unit or contract test. **Verify every
fix end-to-end** — "it compiles" and "my new test passes" are necessary, not
sufficient. Reproduce the original failure and show it is gone.

## Style

- Python 3.11+. Type hints on every new/modified public signature.
- Google-style docstrings on every module, class, and non-trivial function.
- `snake_case` everywhere; CamelCase only for classes and type aliases.
- No bare `except:`, no wildcard imports, no `sys.path` manipulation.
- Parameterized SQL only — no f-string SQL.
- `print()` only in `cli/`, `display/`, and `installer/`; elsewhere emit events
  or log via `PipelineLogger`.

## Extending Ovid2 (the pluggable seams)

Most additions are "drop a file + register in a dict":

| To add a… | Do this |
| --- | --- |
| Driver | New folder `src/ovid2/drivers/<type>/` + entry in `drivers/registry.py::DRIVERS` |
| Connection type | Class + entry in `connections/registry.py::CONNECTION_TYPE_MAP` (+ field specs) |
| Engine | `core/engines/<type>.py` + `ENGINE_TYPE_MAP` + `config/engines.py` mirror |
| CDM | YAML tree under `resources/cdms/<cdm>/` + ~5-line `cdm/<cdm>.py` |
| Formatter | A YAML spec selecting a framework adapter (avoid new `*_formatter.py`) |
| Mapper op | Subclass `BaseMapOp` + entry in `mappers/registry.py::MAP_OPS` |
| Transform op | Subclass `BaseTransform` + entry in `transforms/registry.py::TRANSFORMS` |
| Fixer | Module in `transformations/fixers/common/` + a scope in `FIXER_SCOPES` |

The corresponding `*_registry_complete` / `*_catalogues_synchronised` contract
tests will keep your addition honest.

## Change discipline (per PR)

In the same commit as a structural change:

1. Update the relevant module.
2. Update or add tests.
3. Append a `## [Unreleased]` entry to `CHANGELOG.md`.
4. Update `docs/` if public behavior changed.
5. Add or adjust a contract test if an invariant changed.

## Where development happens

The public GitHub repository
([`uf-hobi-informatics-lab/Ovid2`](https://github.com/uf-hobi-informatics-lab/Ovid2))
is a **mirror**: its `dev` branch is regenerated from an upstream repository on
every upstream push, with site-specific pipelines and internal notes removed.
Because of that:

- Open PRs against **`dev`** on GitHub. Maintainers apply accepted changes
  upstream; they reappear on `dev` in the next sync (your PR is then closed
  with a link to the sync commit).
- Do not rely on `dev` history being linear with your branch — rebase onto
  the latest `dev` before opening or updating a PR.
- Site pipeline packages are deliberately not in this repository; ship your
  own in a separate `ovid2_pipelines/` directory on `PYTHONPATH`.

Maintainers with upstream access: see *Publishing to the public mirror* in
the upstream README (`scripts/public/`).

## Pull requests

- Branch from `dev`; keep PRs focused.
- Fill out the PR template checklist.
- Ensure CI is green before requesting review.
- By contributing, you agree your contributions are licensed under the project
  license (see [`LICENSE`](LICENSE)).

## Code of conduct

Participation is governed by [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
