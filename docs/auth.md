# Ovid2 web panel authentication + RBAC — operator guide

The Ovid2 web control panel supports **Google / Microsoft single sign-on**
with role-based access control. It ships **OFF by default**: until you enable
it and configure a provider, the panel behaves exactly as before (open access
behind the network boundary / reverse proxy). This guide walks through turning
it on.

See the design rationale in
the adr-web-auth-rbac design note.

## Concepts in 30 seconds

- **Login is delegated to your IdP** (Google or Microsoft) over OIDC. There
  are no username/password accounts for regular users.
- **Four roles**, each a superset of the one below it:
  `guest` ⊂ `viewer` ⊂ `pipeline_operator` ⊂ `system_admin`.
- **New users start `pending`** — they can sign in but see nothing until a
  `system_admin` activates them and assigns a role.
- **One sealed break-glass admin** + **host CLI recovery commands** mean you
  can never lock yourself out.
- **Short sessions** (8h absolute / 45min idle) + a silent recheck drop an
  idle or signed-out user back to the login page.

## 1. Register an OIDC application with your IdP

Create an OAuth/OIDC "web application" client with your provider and note its
**client id** and **client secret**.

- **Redirect URI (all providers):**
  `https://ovid2.<your-domain>/auth/callback/<provider-key>`
  where `<provider-key>` is the key you choose in step 2 (e.g. `google` or
  `microsoft`). Example: `https://ovid2.example.edu/auth/callback/google`.
- **Microsoft front-channel logout URL** (optional, for best-effort
  IdP-initiated logout): `https://ovid2.<your-domain>/auth/frontchannel-logout`.
- **Scopes:** `openid email profile` (the default).

> The panel binds to loopback and is fronted by the shared reverse proxy on
> 443 (see `2026-08-24-web-tls-via-shared-reverse-proxy.md`). Use the public
> `https://` FQDN in the redirect URI, not the loopback address.

## 2. Configure the provider in `settings.json`

Auth config lives under `auth` in the panel's `settings.json`
(`$XDG_CONFIG_HOME/ovid2/settings.json`). **No secrets go here** — only the
non-secret provider metadata:

```jsonc
{
  "auth": {
    "enabled": true,
    "providers": {
      "google": {
        "display_name": "Google",
        "issuer": "https://accounts.google.com",
        "client_id": "1234567890-abcdef.apps.googleusercontent.com",
        "scopes": ["openid", "email", "profile"],
        "enabled": true
      },
      "microsoft": {
        "display_name": "Microsoft",
        "issuer": "https://login.microsoftonline.com/<tenant-id>/v2.0",
        "client_id": "00000000-0000-0000-0000-000000000000",
        "enabled": true
      }
    },
    "bootstrap_admin_emails": ["you@your-domain.edu"],
    "absolute_ttl_minutes": 480,
    "idle_ttl_minutes": 45,
    "silent_recheck": true,
    "trust_forwarded_proto": true,
    "service_principal_role": "pipeline_operator"
  }
}
```

Only the `issuer` is needed — Ovid2 fetches the provider's endpoints from
`<issuer>/.well-known/openid-configuration` at runtime. No Google/Microsoft
URLs are hardcoded.

## 3. Store the secrets in `secrets.d/`

All secrets are JSON files under `$XDG_CONFIG_HOME/ovid2/secrets.d/`, mode
**`0o600`** (the panel refuses to read a world-readable secret). One file per
concern:

| File | Shape | Purpose |
| --- | --- | --- |
| `auth_<provider>.json` | `{"client_secret": "…"}` | OIDC client secret, per provider key (e.g. `auth_google.json`) |
| `auth_session.json` | `{"signing_key": "…"}` | Session-cookie / CSRF signing key — **auto-generated on first use** if absent |
| `auth_breakglass.json` | `{"password_hash": "…"}` | Break-glass admin argon2 hash — set via the CLI, not by hand |
| `auth_service_tokens.json` | `{"<sha256hex>": "<role>"}` | MCP service-principal token→role map |

Example (create with a restrictive umask):

```console
$ install -m 600 /dev/null "$XDG_CONFIG_HOME/ovid2/secrets.d/auth_google.json"
$ cat > "$XDG_CONFIG_HOME/ovid2/secrets.d/auth_google.json" <<'JSON'
{"client_secret": "GOCSPX-your-google-client-secret"}
JSON
$ chmod 600 "$XDG_CONFIG_HOME/ovid2/secrets.d/auth_google.json"
```

A provider whose secret file is missing/unreadable is **skipped** with a logged
warning — the login page simply won't show that button, rather than crashing.

## 4. Bootstrap the first admin

Two ways, use either:

- **Allowlist (self-service):** put your email in
  `auth.bootstrap_admin_emails`. The first time you sign in through the IdP
  with that email, you are auto-activated as `system_admin`.
- **Host CLI (no browser needed):**

  ```console
  $ ovid2 web grant-admin you@your-domain.edu
  ```

  If no user row exists yet, this creates a placeholder that your next SSO
  login binds to your IdP identity.

If auth is enabled but there are **zero** active admins and the allowlist is
empty, the `/login` page shows a clear "no admin configured" notice pointing
you at these two options.

## 5. Manage users

Once you are an admin, the **Access → Users** page (`/users`) lists everyone,
highlights **pending approvals**, and lets you activate a user (choosing a
role), change a role, or disable/re-enable an account. The same actions are
available over REST at `/api/v1/users*` (all require the `MANAGE_USERS`
capability, i.e. `system_admin`).

The last active `system_admin` cannot be demoted or disabled — the panel
returns a `409` to prevent a self-lockout.

## 6. Recovery commands (host CLI)

These run on the host with direct DB + secrets access and do **not** require
the web server to be running:

```console
$ ovid2 web grant-admin <email>       # upsert + activate a system_admin
$ ovid2 web list-admins               # list active system administrators
$ ovid2 web set-role <email> <role>   # change a user's role
$ ovid2 web reset-break-glass         # (re)set the sealed local-admin password
```

`reset-break-glass` prompts for a new password and stores only its argon2 hash
in `secrets.d/auth_breakglass.json`. When configured, a **Break-glass** form
appears on `/login` for emergency admin access without the IdP.

## 7. Configure the MCP / agent token (optional)

The MCP server authenticates to the API with a fixed **service-principal
bearer token**:

1. Choose a strong random token and compute its SHA-256 hex digest.
2. Map the digest to a role in `secrets.d/auth_service_tokens.json`:

   ```json
   {"<sha256hex-of-token>": "pipeline_operator"}
   ```

3. Give the plaintext token to the MCP process via `OVID2_MCP_API_TOKEN`.

The mapped role defaults to `pipeline_operator` and can **never** silently
escalate to `system_admin`. The MCP client sends it as
`Authorization: Bearer <token>` on every request.

## Roles → capabilities

| Capability | guest | viewer | operator | admin |
| --- | :-: | :-: | :-: | :-: |
| View public / CDMs / health / API docs | ✓ | ✓ | ✓ | ✓ |
| View runs, run reports | | ✓ | ✓ | ✓ |
| View pipeline data, submit/cancel jobs | | | ✓ | ✓ |
| Manage config / secrets / users, settings, install | | | | ✓ |

Pending users have **no** capabilities until activated.

## Turning it off again

Set `auth.enabled` to `false` (or remove the `auth` block) and restart the
panel. Behavior reverts to fully-open, exactly as before auth existed.
