---
title: Testing
summary: Test layers and markers, fixtures, and the rule that every fix is verified end to end before it is called done.
audience: [developer]
---

## Markers

| Marker | Purpose | Default suite |
| --- | --- | --- |
| `unit` | fast, in-process, single class or function | ✓ |
| `contract` | repository invariants | ✓ |
| `integration` | multi-component, needs a Spark session | CI |
| `smoke` | end-to-end sanity | — |
| `slow` | more than a few seconds | — |
| `real_connection` / `real_cluster` | live services; opt-in | — |
| `perf` / `endurance` | benchmarks | — |

```bash
uv run pytest -m "unit or contract"
uv run pytest tests/contract/                    # all invariants
uv run pytest tests/unit/web -q                  # one area
uv run pytest -m integration                     # needs Java + pyspark
```

## Fixtures and helpers

- `spark` fixture (in `tests/conftest.py`): a local session for
  integration tests.
- `tests/helpers/inline_builders.py`: build small DataFrames inline for
  unit tests.
- `web_client` (in `tests/unit/web/conftest.py`): a FastAPI `TestClient`
  over an isolated XDG tree.

## Verify before you say "done"

Every fix needs all three:

1. **Targeted tests pass**, including any new ones you wrote.
2. **Neighbouring tests still pass**: the module you touched, plus the
   full `tests/contract/` suite.
3. **The original failure is gone**, reproduced through the same surface
   that failed. For example:
   - web: a request to the running server;
   - CLI: a real invocation;
   - pipeline: a real job submitted through `/submit`, `ovid2 etl` or MCP.

If the fix touches code that runs inside the container, **rebuild the
image** before verifying. See [The Docker image](../08-operations/docker-image.md).

If a fix can't be verified end to end (no credentials, a service you
don't own), say so explicitly: name the remaining failure mode and what
it would take to verify it.

## Required tests

Any change to `src/ovid2/core/`, `transformations/` or `formatters/` needs
a unit or contract test in the same commit. A change to an invariant needs
an updated contract test and an ADR.
