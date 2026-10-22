"""Contract: the ovid2 API (web gateway) holds no plaintext secrets.

In the harness / API / executor trust model
(the adr-harness-api-executor-trust-model design note)
the web tier is the *capability broker*. It authenticates, authorizes,
and audits every request, and it may *write* secret files (0o600, via
``secrets_svc``) — but it must never resolve a secret **value** into a
request handler's process memory. Credential-bearing operations
(connection probes, storage browsing, data movement) are delegated to
the executor trust domain (a short-lived ``ovid2 tools …`` subprocess),
so a bug or SSRF in the long-lived web process cannot exfiltrate live
credentials.

Concretely: no file under ``src/ovid2/web/`` may call
``resolve_secrets(...)`` or ``load_secret(...)`` (direct reads), nor
``load_connection(...)`` (which resolves secrets transitively). The one
module allowed to read secret *values* is ``secrets_svc`` itself — but
only via the executor-facing merge tool, never returning them to a
request handler.

Connection probes and storage browsing are delegated to a short-lived
``ovid2 tools probe-connection`` / ``ovid2 tools browse-storage``
subprocess (the executor trust domain) via
``ovid2.web.services._broker``.

Control-plane self-secrets vs. data-plane credentials
-----------------------------------------------------
There is one principled, narrow exception. The web tier owns
*control-plane auth material* that is intrinsic to authenticating its
own requests and gains **no** containment from brokering to a
subprocess:

* the OIDC RP **client secret** (needed in-process for the
  authorization-code token exchange — the resulting IdP tokens land
  back in the web process regardless of where the secret sits, so a
  subprocess is pure ceremony);
* the session-cookie **signing key** (the web tier's *own* symmetric
  key, used to sign/verify every request — delegating it per request is
  absurd);
* the break-glass admin **argon2 hash** (a verifier, not a usable
  credential — reading it leaks nothing).

These live under the reserved ``auth_`` secret-name namespace
(``auth_<provider>``, ``auth_session``, ``auth_breakglass``,
``auth_service_tokens``) and MAY be read in-process by
``src/ovid2/web/auth/*`` modules. The exemption is
pinned to that name prefix so it cannot be abused to smuggle a
data-plane credential read back into the web tier: a ``load_secret``
call in ``web/auth/*`` resolving to a non-``auth_`` name still fails,
and ``load_connection`` (the transitive third-party-credential path)
stays banned web-wide with no exception anywhere. See
``adr-harness-api-executor-trust-model.md`` §"Control-plane
self-secrets vs data-plane credentials".
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: Function names that dereference a credential into memory.
SECRET_READ_CALLS: frozenset[str] = frozenset(
    {"resolve_secrets", "load_secret", "load_connection"}
)

#: Reserved secret-name prefix for the web tier's own control-plane auth
#: material (OIDC client secrets, session signing key, break-glass hash).
_AUTH_SECRET_PREFIX: str = "auth_"

#: Web-relative module paths permitted to read ``auth_``-prefixed secrets
#: in-process (never ``load_connection``, never a non-``auth_`` name).
_AUTH_SECRET_MODULES: frozenset[str] = frozenset(
    {
        "auth/oidc.py",
        "auth/cookie.py",
        "auth/breakglass.py",
        # ``auth_service_tokens`` is a {sha256(token): role} verifier map —
        # a control-plane self-secret in the same category as the
        # break-glass argon2 hash: reading it leaks no usable data-plane
        # credential (it only maps a presented token digest onto a role).
        "auth/service_token.py",
    }
)


def _call_name(node: ast.Call) -> str | None:
    """Return the bare callee name for a call node."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _auth_secret_var_names(tree: ast.Module) -> frozenset[str]:
    """Collect local names provably assigned from an ``auth_`` name builder.

    A statement ``x = secret_name_for(provider)`` (or any call to a helper
    whose name mentions ``secret_name``) proves ``x`` holds an
    ``auth_``-prefixed secret name — the builder is the only thing that
    constructs those names. Such names are safe to pass to ``load_secret``
    under the auth exemption; any other variable is not.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not isinstance(value, ast.Call):
            continue
        helper = _call_name(value)
        if helper is None or "secret_name" not in helper:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return frozenset(names)


def _resolves_only_auth_secret(node: ast.Call, auth_var_names: frozenset[str]) -> bool:
    """Return whether a ``load_secret`` call targets an ``auth_`` name.

    The exemption is granted only when the first positional argument is a
    literal ``auth_``-prefixed string, a module-level ``*SECRET_NAME``
    constant, an inline ``secret_name_for(...)`` call, or a local variable
    provably assigned from that builder (``auth_var_names``). A dynamic /
    unknown argument does **not** qualify — the call must fail so an
    attacker cannot smuggle an arbitrary secret name through the auth
    exemption.
    """
    if not node.args:
        return False
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value.startswith(_AUTH_SECRET_PREFIX)
    if isinstance(first, ast.Call):
        helper = _call_name(first)
        return helper is not None and "secret_name" in helper
    if isinstance(first, ast.Name):
        return (
            "SECRET_NAME" in first.id
            or first.id.upper().startswith("AUTH_")
            or first.id in auth_var_names
        )
    if isinstance(first, ast.Attribute):
        return "SECRET_NAME" in first.attr or first.attr.upper().startswith("AUTH_")
    return False


@pytest.mark.contract
def test_web_never_resolves_plaintext_secrets(src_root: Path) -> None:
    """No web module may call resolve_secrets() / load_secret().

    The single, name-pinned exception: ``web/auth/*`` may call
    ``load_secret`` for a reserved ``auth_``-prefixed control-plane
    self-secret. ``load_connection`` is never exempt, anywhere.
    """
    web_root = src_root / "web"
    if not web_root.is_dir():
        pytest.skip("Web tier not present.")

    offenders: list[str] = []
    for py in web_root.rglob("*.py"):
        rel_to_web = py.relative_to(web_root).as_posix()
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        auth_var_names = _auth_secret_var_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name not in SECRET_READ_CALLS:
                continue
            if (
                name == "load_secret"
                and rel_to_web in _AUTH_SECRET_MODULES
                and _resolves_only_auth_secret(node, auth_var_names)
            ):
                continue
            offenders.append(
                f"{py.relative_to(src_root)}:{node.lineno} calls {name}()"
            )

    assert not offenders, (
        "Web tier (ovid2 API) must not resolve plaintext secrets in a "
        "request handler; delegate to an executor subprocess "
        "(see adr-harness-api-executor-trust-model.md). The only allowed "
        "in-process reads are ``auth_``-prefixed control-plane self-secrets "
        "from web/auth/*:\n"
        + "\n".join(offenders)
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
