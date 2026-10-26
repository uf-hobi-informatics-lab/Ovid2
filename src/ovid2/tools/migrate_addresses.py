"""Migrate legacy address configs to the W7 connection-shape.

Legacy configs blend credentials with per-run location under one
``src_args`` dict::

    {"src_name": "snowflake",
     "src_args": {"account": "…", "user": "…", "password": "…",
                  "database": "…", "schema": "…"}}

W7 splits the two: a shared **connection** (credentials + shared
endpoint metadata) plus one **address** per per-run location. This
tool walks every discoverable legacy address config and produces:

1. A new connection JSON under ``$XDG_CONFIG_HOME/ovid2/connections/``.
2. A per-address secret file under ``$XDG_CONFIG_HOME/ovid2/secrets.d/``
   with mode 0o600 (see :mod:`ovid2.web.services.secrets_svc`).
3. A rewritten address JSON with the new ``{connection, location}``
   shape.

Runs in dry-run mode by default and prints a unified diff of the
proposed changes; pass ``--write`` to actually persist them.

Usage::

    ovid2 tools migrate-addresses [--write] [--only NAME]

The tool is idempotent: addresses already in the W7 shape are skipped.
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import sys
from dataclasses import dataclass
from typing import Any

from ovid2.config import addresses as _addresses
from ovid2.config import paths as _paths

logger = logging.getLogger(__name__)


#: Per-legacy-type: (connection args keys, secret keys, location keys).
#:
#: Any key not listed drops through into ``location`` verbatim.
#:
#: For ``disc`` the legacy ``path`` was the root of the mount — moving
#: it into the connection is the whole point of the migration, so it
#: becomes ``host_path`` on the connection side (widened schema from
#: the disc-mount / path-picker phase) and the address is left with
#: an empty location. Since 2026-10 an address is the *exact* location
#: of the data (no ``{pipeline}/{batch_id}`` is appended at read time),
#: so a migrated root-level address reads files directly under the root;
#: point ``location.path`` at the batch folder to read a specific batch.
_SPLIT: dict[str, tuple[frozenset[str], frozenset[str], frozenset[str]]] = {
    "disc": (
        frozenset({"host_path"}),
        frozenset(),
        frozenset({"format", "lowercase_columns"}),
    ),
    "snowflake": (
        frozenset({"account", "user", "warehouse", "role", "authenticator"}),
        frozenset({"password", "private_key", "private_key_passphrase"}),
        frozenset({"database", "schema"}),
    ),
}

#: Legacy key names that should be *renamed* into the connection args
#: for a given type. Used to map ``disc.src_args.path`` → connection
#: ``host_path`` during migration.
_LEGACY_RENAMES: dict[str, dict[str, str]] = {
    "disc": {"path": "host_path"},
}


@dataclass(frozen=True)
class MigrationPlan:
    """One planned migration for a single legacy address."""

    address_name: str
    connection_name: str
    secret_ref: str | None
    old_address: dict[str, Any]
    new_address: dict[str, Any]
    new_connection: dict[str, Any]
    new_secret: dict[str, Any]


def _split_src_args(
    src_name: str,
    src_args: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return ``(connection_args, secret_values, location)``.

    Unknown keys fall through to ``location`` so nothing is silently
    dropped; the operator sees them in the diff and can decide.
    """
    conn_keys, sec_keys, loc_keys = _SPLIT.get(
        src_name,
        (frozenset(), frozenset(), frozenset()),
    )
    renames = _LEGACY_RENAMES.get(src_name, {})
    conn_args: dict[str, Any] = {}
    secret_values: dict[str, Any] = {}
    location: dict[str, Any] = {}
    for key, value in src_args.items():
        target_key = renames.get(key, key)
        # A rename lands the value in the connection's args regardless
        # of the split-set membership; explicit rename overrides.
        if key in renames:
            conn_args[target_key] = value
            continue
        if target_key in conn_keys:
            conn_args[target_key] = value
        elif target_key in sec_keys:
            if value not in (None, ""):
                secret_values[target_key] = value
        elif target_key in loc_keys:
            location[target_key] = value
        else:
            location[target_key] = value
    return conn_args, secret_values, location


def _connection_name(address_name: str, src_name: str, conn_args: dict[str, Any]) -> str:
    """Pick a sensible connection name for a legacy address.

    For Snowflake we prefer ``sf_<account>_<user>`` when both are known
    so two addresses pointing at the same account share one connection.
    For disc we key off ``host_path`` so ``/data/in`` and ``/data/out``
    become distinct but deterministic connection names. Everything
    else falls back to ``conn_<address>``.
    """
    if src_name == "snowflake" and conn_args.get("account") and conn_args.get("user"):
        stem = f"sf_{conn_args['account']}_{conn_args['user']}"
    elif src_name == "disc" and conn_args.get("host_path"):
        # Use the last two path components as a hint so multiple
        # pipelines rooted at ``/data`` still collide (which is what we
        # want — one shared connection). Two different roots produce
        # two names.
        parts = [p for p in str(conn_args["host_path"]).split("/") if p]
        tail = "_".join(parts[-2:]) if parts else "root"
        stem = f"disc_{tail}"
    else:
        stem = f"conn_{address_name}"
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in stem)


def _plan_one(address_name: str) -> MigrationPlan | None:
    """Return a :class:`MigrationPlan` for one address, or ``None`` when skipped."""
    try:
        cfg = _addresses.load_address_config(address_name)
    except (FileNotFoundError, ValueError):
        return None
    if cfg.get("_shape") == "connection":
        return None
    src_name = str(cfg.get("src_name") or "")
    src_args = dict(cfg.get("src_args") or {})
    if src_name not in _SPLIT:
        logger.warning("Skipping '%s': unknown legacy src_name '%s'.", address_name, src_name)
        return None
    conn_args, secret_values, location = _split_src_args(src_name, src_args)
    conn_name = _connection_name(address_name, src_name, conn_args)
    secret_ref: str | None = None
    if secret_values:
        secret_ref = f"conn_{conn_name}"
    new_connection: dict[str, Any] = {"type": src_name, "args": conn_args}
    if secret_ref:
        new_connection["secret_ref"] = secret_ref
    new_address: dict[str, Any] = {"connection": conn_name, "location": location}
    old_address_wrapped: dict[str, Any] = {
        "src_name": src_name,
        "src_args": src_args,
    }
    return MigrationPlan(
        address_name=address_name,
        connection_name=conn_name,
        secret_ref=secret_ref,
        old_address=old_address_wrapped,
        new_address=new_address,
        new_connection=new_connection,
        new_secret=secret_values,
    )


def _pretty(payload: dict[str, Any]) -> str:
    """Return a human-friendly JSON dump."""
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def _diff(name: str, before: dict[str, Any], after: dict[str, Any]) -> str:
    """Return a unified diff between two JSON documents."""
    return "".join(
        difflib.unified_diff(
            _pretty(before).splitlines(keepends=True),
            _pretty(after).splitlines(keepends=True),
            fromfile=f"{name} (before)",
            tofile=f"{name} (after)",
        )
    )


def _redact_secret(payload: dict[str, Any]) -> dict[str, Any]:
    """Mask every value in a secret dict for stdout printing."""
    return dict.fromkeys(payload, "***")


def _apply(plan: MigrationPlan) -> None:
    """Actually persist a migration plan to disk."""
    from ovid2.web.models import ConnectionWrite, SecretWrite
    from ovid2.web.services import connections_svc, secrets_svc

    # 1. Secret file (if any). ConnectionWrite would do this too, but
    # we want the exact secret_ref chosen by the plan.
    if plan.secret_ref and plan.new_secret:
        try:
            secrets_svc.write_secret(
                plan.secret_ref,
                SecretWrite(values=plan.new_secret),
                actor="migrate-addresses",
            )
        except secrets_svc.SecretError as exc:
            raise SystemExit(f"Refusing to migrate {plan.address_name}: {exc}") from exc

    # 2. Connection file.
    conn_payload = ConnectionWrite(
        type=str(plan.new_connection["type"]),
        args=dict(plan.new_connection.get("args") or {}),
        secrets={},  # secret file already written above
        secret_ref=plan.secret_ref,
    )
    try:
        connections_svc.write_connection(
            plan.connection_name,
            conn_payload,
            actor="migrate-addresses",
        )
    except connections_svc.ConnectionError as exc:
        raise SystemExit(f"Refusing to migrate {plan.address_name}: {exc}") from exc

    # 3. Address file — write directly (not via addresses_svc.write_address
    # which enforces a stricter schema). The address JSON lands in the
    # user-scoped configs directory.
    target = _paths.configs_dir() / f"{plan.address_name}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(_pretty(plan.new_address), encoding="utf-8")
    tmp.chmod(0o644)
    tmp.replace(target)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``tools migrate-addresses`` subcommand."""
    parser = subparsers.add_parser(
        "migrate-addresses",
        help="Convert legacy address configs to the W7 connection-shape.",
        description=__doc__,
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually persist the migration (default: dry-run).",
    )
    parser.add_argument(
        "--only",
        metavar="NAME",
        action="append",
        default=[],
        help="Limit the migration to specific address names (repeatable).",
    )
    parser.set_defaults(_entry=main)


def main(args: argparse.Namespace) -> int:
    """Entry point for ``ovid2 tools migrate-addresses``."""
    only = set(getattr(args, "only", []) or [])
    names = _addresses.list_available_addresses()
    if only:
        names = [n for n in names if n in only]
    if not names:
        print("No address configs to migrate.")
        return 0
    plans: list[MigrationPlan] = []
    for name in names:
        plan = _plan_one(name)
        if plan is None:
            print(f"[skip] {name}: already migrated or unsupported.")
            continue
        plans.append(plan)

    if not plans:
        print("Every address is already on the W7 shape.")
        return 0

    for plan in plans:
        print(f"\n=== {plan.address_name} ===")
        print(_diff(plan.address_name + ".json", plan.old_address, plan.new_address))
        print(f"--- connection: {plan.connection_name} ---")
        print(_pretty(plan.new_connection))
        if plan.new_secret:
            print(f"--- secret: {plan.secret_ref} (values redacted) ---")
            print(_pretty(_redact_secret(plan.new_secret)))
        if plan.address_name in {
            p.address_name for p in plans if p is not plan
        }:  # pragma: no cover — defensive
            print("!! duplicate plan for same address; aborting.", file=sys.stderr)
            return 1

    if not args.write:
        print(
            f"\nDry-run complete. {len(plans)} address(es) would be migrated. "
            "Re-run with --write to persist.",
        )
        return 0

    for plan in plans:
        _apply(plan)
        print(f"[write] migrated {plan.address_name}.")
    print(f"\nMigrated {len(plans)} address(es).")
    return 0
