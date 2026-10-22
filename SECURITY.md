# Security Policy

Ovid2 is a data-processing engine used with healthcare pipeline data (PCORnet,
OMOP). Security and data-confidentiality issues are treated as high priority.

## Reporting a vulnerability

**Do not open a public issue for a security vulnerability.**

Report privately to the maintainers:

- Use GitHub's private
  [security advisory](https://github.com/uf-hobi-informatics-lab/Ovid2/security/advisories/new)
  workflow on the public repository, or
- email the maintainers listed in `pyproject.toml`.

Please include:

- A description of the issue and its impact.
- Steps to reproduce (a minimal proof of concept if possible).
- Affected version / commit.

We aim to acknowledge reports within a few business days and to keep you
informed as we investigate and remediate. Please give us reasonable time to
release a fix before any public disclosure.

## Never include secrets or patient data

When filing **any** issue, PR, or report:

- Do **not** paste credentials, API tokens, connection strings, private keys,
  or per-pipeline salts.
- Do **not** include real patient data, PHI, or any identifiable records.
  Reduce reproductions to synthetic, non-sensitive inputs.
- Scrub logs and tracebacks of hostnames, paths, and account identifiers before
  sharing.

Ovid2's pre-commit config runs `detect-private-key`, but that is a backstop,
not a guarantee — the responsibility to keep secrets out of the repository is
the contributor's.

## Security model (context for reporters)

Ovid2 is built around a three-tier trust separation. Understanding it helps
classify a report:

- **Agent Ovid (MCP server, `src/ovid2/mcp/`)** — untrusted planner. Speaks only
  the typed HTTP capability API; imports no secret / driver / connection /
  pipeline module. A compromised or prompt-injected agent must not be able to
  reach a credential.
- **ovid2 API (`src/ovid2/web/`)** — trusted gateway. Authorizes, audits, and
  gates; never resolves a plaintext secret inside a request handler.
- **Executor (`cli` / `core` / `drivers`)** — the only tier that dereferences
  credentials, running least-privilege (allow-listed env + run-scoped secret
  mounts). Destructive operations require a human-approval gate.

Reports that demonstrate a break in these boundaries (e.g. the agent tier
reaching a secret, or a request handler reading plaintext credentials) are
especially valuable. These boundaries are enforced by contract tests
(`tests/contract/test_agent_tier_capability_only.py`,
`test_web_no_plaintext_secret_reads.py`,
`test_destructive_ops_require_approval.py`); a bypass of any of them is a
security issue.

## Supported versions

Ovid2 is pre-1.0 (alpha). Security fixes are applied to `main`. There is no
long-term-support branch yet; pin to a commit and upgrade forward.
