---
title: Secrets
summary: Where secrets live, how each executor sees only its own, and how to move from warn to enforce mode.
audience: [admin, developer]
---

## Layout

Every secret is a JSON file in `$XDG_CONFIG_HOME/ovid2/secrets.d/`. It
must be **mode 0600** and owned by the running user. The loader refuses
group- or world-readable files, and `ovid2 doctor` reports them.

| File | Used by |
| --- | --- |
| `<connection secret_ref>.json` | a storage account's credentials (keys, passwords, tokens) |
| `<engine secret_ref>.json` | an engine's credentials, e.g. Databricks |
| `<pipeline>.json` | that pipeline's `hash_id` salt (`id_salt`) |
| `llm_<name>.json` | an assistant model's API key |
| `auth_*.json` | SSO, sessions, break-glass, service tokens |
| `snowflake.json` | the default Snowflake key-pair (from `ovid2 init`) |

Use **Configure → Secrets** to create and update secrets. Values are
never displayed.

## Scoped executors

No process gets the whole `secrets.d/`. Each executor subprocess (job,
connection test, storage browse, Data Space sample, LLM turn) receives:

- an **allow-listed environment**, with tokens and `OVID2_SECRETS_FILE`
  stripped;
- a private `0700` directory, exported as `OVID2_SECRETS_DIR`, holding
  `0600` copies of only the secrets its request names:

  | Request names | Secret granted |
  | --- | --- |
  | an address | its connection's `secret_ref` |
  | a connection | its `secret_ref` |
  | the engine | its `secret_ref`, and those of connections in its args |
  | the pipeline | `<pipeline>.json` (salt), plus the XOR seed for pipeline runs |

Inside containers, the grant is mounted read-only at
`/ovid2-scoped-secrets`. The directory is deleted when the child exits;
leftovers are swept when `ovid2 web` starts.

## Warn → enforce

| `OVID2_SECRETS_SCOPE_MODE` | Behaviour |
| --- | --- |
| `warn` (default) | an out-of-scope secret still resolves, with a warning: *"Secret 'x' is outside this executor's declared scope …"* |
| `enforce` | only the scoped directory is searched. A miss raises `SecretNotFoundError … The run was not granted it.` Containers, including every Spark node, see an empty tmpfs over `secrets.d/`. |

To roll out:

1. Run in `warn` for a full cycle of your jobs.
2. Search the logs: `journalctl -u ovid2-web -u ovid2-web-worker | grep "outside this executor's declared scope"`.
3. Fix whatever the warnings name. Usually a connection is missing its
   `secret_ref`.
4. Set `OVID2_SECRETS_SCOPE_MODE=enforce` in `/etc/ovid2/web.env` and
   restart.

> [!NOTE]
> On the host, scoping makes sure correct code uses only what it was
> granted, but it isn't a sandbox: subprocesses run as the web tier's
> uid. Containers in `enforce` mode are truly isolated.

## Never

- Never put a credential in a connection, address, pipeline or settings
  JSON. Those files hold a `secret_ref`, not the secret.
- Never commit `secrets.d/`.
- Back up pipeline salts: losing one changes every hashed identifier.
