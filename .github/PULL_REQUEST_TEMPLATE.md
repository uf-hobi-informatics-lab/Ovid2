<!--
Thanks for contributing! Please read AGENTS.md and CONTRIBUTING.md first.
Do NOT include credentials, tokens, or real patient data in this PR.
-->

## Summary

<!-- What does this change do, and why? -->

## Related issues

<!-- e.g. Closes #123 -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor / internal
- [ ] Docs
- [ ] Build / CI / tooling

## Checklist

- [ ] I read `AGENTS.md` and followed its design principles and style rules.
- [ ] `ruff check .` and `ruff format --check .` pass.
- [ ] `mypy --config-file=pyproject.toml src/ovid2` passes.
- [ ] `pytest -q -m contract` passes.
- [ ] Relevant unit tests pass (`pytest -q -m "not contract and not integration and not real_connection and not real_cluster and not perf and not endurance"`).
- [ ] I added or updated tests for my change (required for `core/`, `transformations/`, `formatters/`).
- [ ] I appended a `## [Unreleased]` entry to `CHANGELOG.md`.
- [ ] I updated `docs/` if public behavior changed.
- [ ] If I changed an invariant, I added/adjusted a contract test.
- [ ] No secrets, credentials, hardcoded hosts/paths, or patient data are included.

## Verification

<!--
Per AGENTS.md "verify every fix": describe how you proved this works
end-to-end, not just that it compiles. If it touches a running surface
(web/MCP/CLI/pipeline), show you exercised that surface. If a failure mode
could not be verified, say so explicitly.
-->
