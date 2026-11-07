---
title: Contributing
summary: Code style, change discipline, commit hygiene, and keeping shared code safe for the public mirror.
audience: [developer]
---

## Style

- **Python 3.11+** syntax. Type hints on every public function;
  `mypy --strict` on `src/ovid2/`.
- **Google-style docstrings** on every module, class and non-trivial
  function.
- **snake_case** everywhere; CamelCase only for classes and type aliases
  (ruff `N`).
- No bare `except:`, no wildcard imports, no `sys.path` manipulation.
- **Parameterised SQL only**. No f-string SQL in `src/ovid2/`.
- `print()` only in `cli/`, `display/` and `installer/`. Elsewhere, emit
  events or log.
- An `if __name__ == "__main__":` guard on every executable module.
- File modes: 0644 for files, 0755 for directories, 0600 for secrets.
- No hard-coded credentials, hosts or absolute paths outside `resources/`
  and configs.

## Change discipline

In the **same commit** as a structural change:

1. Update the module.
2. Add or update tests.
3. Add an entry under `## [Unreleased]` in `CHANGELOG.md`.
4. If public behaviour changed, update the manual
   (`src/ovid2/resources/manual/`) and regenerate the reference with
   `ovid2 manual gen-reference`.
5. If an invariant changed, add or adjust a contract test and write an
   ADR.

## Keep shared code site-neutral

A sanitised snapshot of the repository is published publicly. Shared
source (`src/`, `tests/`, `docs/`, the manual) must not contain
institution e-mails, hosts, IPs or paths, network or datamart ids, model
gateway URLs, or site names. Use `demo`, `example.org` and `<account>` in
examples. A leak gate blocks any publish that matches a denied pattern.

## Forbidden

- Copy-pasting formatter or fixer boilerplate. Use the adapter pattern.
- New hand-written `*_formatter.py` files where a YAML spec would do.
- Per-pipeline adapters.
- Committing secrets, credentials or pipeline data files.

## Reporting security issues

Report vulnerabilities privately, as described in `SECURITY.md`. Never put
credentials, PHI or real patient data in an issue, pull request or log.
