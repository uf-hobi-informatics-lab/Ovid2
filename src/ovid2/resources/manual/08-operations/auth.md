---
title: Authentication & roles
summary: Turn on Google / Microsoft single sign-on, bootstrap the first admin, manage roles, and recover from lock-out.
audience: [admin]
---

The panel supports **OIDC single sign-on** (Google, Microsoft, or any
OIDC provider) with role-based access control. It ships **off**: until
you enable it, anyone who can reach the port has full control.

## Concepts

- **Sign-in is delegated** to your identity provider. Ordinary users
  have no local passwords.
- **Four roles**, each including the one below:
  `guest` ⊂ `viewer` ⊂ `pipeline_operator` ⊂ `system_admin`.
- **New users start `pending`.** They can sign in but see nothing until
  an admin activates them.
- **One sealed break-glass admin**, plus host CLI recovery, means you
  can't lock yourself out.
- **Short sessions:** 8 h absolute, 45 min idle, with a silent recheck.

## 1. Register an OIDC client

With your provider, create a *web application* client and note its
**client id** and **client secret**.

- Redirect URI: `https://ovid2.example.org/auth/callback/<provider-key>`
  (the key you choose in step 2, e.g. `google`).
- Optional Microsoft front-channel logout:
  `https://ovid2.example.org/auth/frontchannel-logout`.
- Scopes: `openid email profile`.

Use the public `https://` hostname of your reverse proxy, not the loopback
address.

## 2. Configure `settings.json`

```jsonc
{
  "auth": {
    "enabled": true,
    "providers": {
      "google":    { "display_name": "Google",
                     "issuer": "https://accounts.google.com",
                     "client_id": "1234567890-abc.apps.googleusercontent.com" },
      "microsoft": { "display_name": "Microsoft",
                     "issuer": "https://login.microsoftonline.com/<tenant-id>/v2.0",
                     "client_id": "00000000-0000-0000-0000-000000000000" }
    },
    "bootstrap_admin_emails": ["you@example.org"],
    "absolute_ttl_minutes": 480,
    "idle_ttl_minutes": 45,
    "silent_recheck": true,
    "trust_forwarded_proto": true,
    "service_principal_role": "pipeline_operator"
  }
}
```

Only the `issuer` is needed. Ovid2 discovers the provider's endpoints at
`<issuer>/.well-known/openid-configuration`.

## 3. Store the secrets

| File in `secrets.d/` | Shape | Purpose |
| --- | --- | --- |
| `auth_<provider>.json` | `{"client_secret": "…"}` | the OIDC client secret |
| `auth_session.json` | `{"signing_key": "…"}` | cookie and CSRF signing (auto-generated) |
| `auth_breakglass.json` | `{"password_hash": "…"}` | set via the CLI only |
| `auth_service_tokens.json` | `{"<sha256hex>": "<role>"}` | MCP service tokens |

```bash
install -m 600 /dev/null "$XDG_CONFIG_HOME/ovid2/secrets.d/auth_google.json"
printf '{"client_secret": "%s"}\n' "<secret>" > "$XDG_CONFIG_HOME/ovid2/secrets.d/auth_google.json"
```

A provider whose secret is missing is skipped, with a logged warning, so
its button doesn't appear.

## 4. Bootstrap the first admin

Use either method:

- **Allowlist:** list your e-mail in `bootstrap_admin_emails`. Your first
  SSO sign-in activates you as `system_admin`.
- **CLI:** `ovid2 web grant-admin you@example.org`. This creates a
  placeholder that your next sign-in adopts.

## 5. Manage users

Use **Access → Users** to activate users, change roles, and disable or
enable accounts. See [Administration](../03-web-panel/administration.md).

## Roles → capabilities

| Capability | guest | viewer | operator | admin |
| --- | :-: | :-: | :-: | :-: |
| View CDMs, health, the manual, API docs | ✓ | ✓ | ✓ | ✓ |
| View runs and reports; use the assistant | | ✓ | ✓ | ✓ |
| View pipeline data, submit and cancel jobs | | | ✓ | ✓ |
| Manage config, secrets, users, LLMs, settings, install; approve | | | | ✓ |

## Recovery

These commands work on the host without the web server:

```bash
ovid2 web grant-admin <email>
ovid2 web list-admins
ovid2 web set-role <email> <role>
ovid2 web reset-break-glass      # then use the Break-glass form on /login
```

## Turning auth off

Set `auth.enabled` to `false` and restart the panel.
