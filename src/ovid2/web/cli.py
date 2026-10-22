"""``ovid2 web`` — launch the FastAPI control panel, plus recovery commands.

Bare ``ovid2 web`` delegates to uvicorn. The bind address defaults to
:data:`ovid2.web.settings.WEB_BIND` (``127.0.0.1:8090``, loopback only —
TLS is terminated by the shared host reverse proxy); ``--bind`` and
``$OVID2_WEB_BIND`` override.

Nested **recovery** subcommands run on the host with direct database and
secrets access (the web server need **not** be running) so an operator can
never be permanently locked out of the admin panel:

- ``ovid2 web grant-admin <email>`` — upsert + activate a ``system_admin``.
- ``ovid2 web list-admins`` — list active system administrators.
- ``ovid2 web set-role <email> <role>`` — change a user's role by email.
- ``ovid2 web reset-break-glass`` — set the sealed local-admin password.

Every mutating recovery command lands an audit row (via ``users_svc`` or
``breakglass.set_password``). Recovery commands print human-facing output
directly (``print`` is reserved for ``cli/`` per AGENTS.md) and never echo
secrets.
"""

from __future__ import annotations

import argparse
import getpass
import logging

from ovid2.web import db as web_db
from ovid2.web.auth import breakglass
from ovid2.web.auth.roles import Role
from ovid2.web.services import users_svc
from ovid2.web.settings import WEB_BIND

logger = logging.getLogger(__name__)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``web`` subcommand (server + recovery actions).

    Bare ``ovid2 web`` runs uvicorn (the default entry). An optional nested
    action subparser adds the recovery commands, each wiring its own
    ``_entry`` handler.

    Args:
        subparsers: The top-level ``_SubParsersAction`` to register onto.
    """
    parser = subparsers.add_parser(
        "web",
        help="Run the web control panel (FastAPI + HTMX).",
        description="Serve the Ovid2 control panel over HTTP.",
    )
    parser.add_argument(
        "--bind",
        default=WEB_BIND,
        help=f"Host:port to bind (default: {WEB_BIND}).",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable uvicorn auto-reload (dev only).",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="uvicorn log level (default: info).",
    )
    # Bare ``ovid2 web`` (no action) boots the server, exactly as before.
    parser.set_defaults(_command="web", _entry=main, web_action=None)

    actions = parser.add_subparsers(
        dest="web_action",
        metavar="ACTION",
        help="Operator recovery commands (run on the host, no server needed).",
    )

    grant = actions.add_parser(
        "grant-admin",
        help="Upsert and activate a user as system_admin (recovery).",
        description=(
            "Grant system_admin to a user by email, creating a pending "
            "placeholder row the next SSO login can adopt if none exists."
        ),
    )
    grant.add_argument("email", help="Email address of the user to promote.")
    grant.set_defaults(_command="web", _entry=grant_admin)

    list_admins_p = actions.add_parser(
        "list-admins",
        help="List active system administrators.",
        description="Print every active system_admin user.",
    )
    list_admins_p.set_defaults(_command="web", _entry=list_admins)

    set_role_p = actions.add_parser(
        "set-role",
        help="Change a user's role by email (recovery).",
        description="Resolve a user by email and set their role.",
    )
    set_role_p.add_argument("email", help="Email address of the target user.")
    set_role_p.add_argument(
        "role",
        help="New role: " + ", ".join(r.value for r in Role) + ".",
    )
    set_role_p.set_defaults(_command="web", _entry=set_role)

    reset_bg = actions.add_parser(
        "reset-break-glass",
        help="Set the sealed break-glass local-admin password (recovery).",
        description=(
            "Prompt for a new break-glass password on stdin and store its "
            "argon2 hash. The password is never echoed or printed."
        ),
    )
    reset_bg.set_defaults(_command="web", _entry=reset_break_glass)


def _find_user_by_email(email: str) -> dict[str, object] | None:
    """Return the user record whose email matches ``email`` (case-insensitive).

    Args:
        email: Email address to search for.

    Returns:
        The matching user record, or ``None`` when no user matches.
    """
    target = email.strip().lower()
    for user in users_svc.list_users():
        user_email = user.get("email")
        if isinstance(user_email, str) and user_email.lower() == target:
            return user
    return None


def grant_admin(args: argparse.Namespace) -> int:
    """Upsert and activate ``args.email`` as a ``system_admin``.

    Creates a pending placeholder row keyed by email
    (``issuer="ovid2:recovery"``, ``subject=email``) that the next SSO login
    can adopt, then activates it as ``system_admin``.

    Args:
        args: Parsed CLI namespace carrying ``email``.

    Returns:
        Process exit code (0 on success, 1 on a user-service error).
    """
    web_db.ensure_schema()
    email = str(args.email).strip()
    record = users_svc.upsert_from_idp(
        issuer="ovid2:recovery",
        subject=email,
        email=email,
        name=None,
    )
    try:
        activated = users_svc.activate(
            user_id=str(record["user_id"]),
            role=Role.SYSTEM_ADMIN,
            actor="cli",
        )
    except users_svc.UserError as exc:
        print(f"[ovid2 web] grant-admin failed: {exc}")
        return 1
    print(
        f"[ovid2 web] Granted system_admin to {email} "
        f"(user_id={activated['user_id']}, status={activated['status']})."
    )
    return 0


def list_admins(args: argparse.Namespace) -> int:
    """Print all active ``system_admin`` users.

    Args:
        args: Parsed CLI namespace (unused).

    Returns:
        Process exit code (always 0).
    """
    del args  # unused
    web_db.ensure_schema()
    admins = [
        user
        for user in users_svc.list_users(status="active")
        if user.get("role") == Role.SYSTEM_ADMIN.value
    ]
    if not admins:
        print("[ovid2 web] No active system administrators.")
        return 0
    print(f"[ovid2 web] Active system administrators ({len(admins)}):")
    for user in admins:
        print(
            f"  - {user.get('email') or '(no email)'}"
            f"  user_id={user['user_id']}  status={user['status']}"
        )
    return 0


def set_role(args: argparse.Namespace) -> int:
    """Change the role of the user identified by ``args.email``.

    Args:
        args: Parsed CLI namespace carrying ``email`` and ``role``.

    Returns:
        Process exit code (0 on success, 2 on an invalid role string, 1 on a
        user-service error such as the last-admin guard, or a missing user).
    """
    web_db.ensure_schema()
    role_str = str(args.role)
    try:
        role = Role(role_str)
    except ValueError:
        valid = ", ".join(r.value for r in Role)
        print(f"[ovid2 web] invalid role '{role_str}' (expected one of: {valid}).")
        return 2

    email = str(args.email).strip()
    user = _find_user_by_email(email)
    if user is None:
        print(f"[ovid2 web] no user found with email '{email}'.")
        return 1

    try:
        updated = users_svc.set_role(
            user_id=str(user["user_id"]),
            role=role,
            actor="cli",
        )
    except users_svc.UserError as exc:
        print(f"[ovid2 web] set-role failed: {exc}")
        return 1
    print(
        f"[ovid2 web] Set role of {email} to {updated['role']} "
        f"(user_id={updated['user_id']})."
    )
    return 0


def reset_break_glass(args: argparse.Namespace) -> int:
    """Prompt for and store a new break-glass local-admin password.

    The password is read from stdin via :func:`getpass.getpass` and is never
    echoed, logged, or printed. Its argon2 hash is persisted through
    :func:`ovid2.web.auth.breakglass.set_password`, which also audits the
    ``breakglass.set`` action.

    Args:
        args: Parsed CLI namespace (unused).

    Returns:
        Process exit code (0 on success, 1 on empty/mismatched input).
    """
    del args  # unused
    password = getpass.getpass("New break-glass password: ")
    if not password:
        print("[ovid2 web] break-glass password must not be empty.")
        return 1
    confirm = getpass.getpass("Confirm break-glass password: ")
    if password != confirm:
        print("[ovid2 web] passwords do not match.")
        return 1
    try:
        breakglass.set_password(password, actor="cli")
    except ValueError as exc:
        print(f"[ovid2 web] reset-break-glass failed: {exc}")
        return 1
    print("[ovid2 web] Break-glass admin password updated.")
    return 0


def main(args: argparse.Namespace) -> int:
    """Boot uvicorn against :data:`ovid2.web.app.app`.

    Args:
        args: Parsed CLI namespace.

    Returns:
        Process exit code.
    """
    try:
        import uvicorn
    except ImportError:
        print(
            "[ovid2 web] uvicorn is not installed. "
            "Install the web extras: `pip install ovid2[web]`.",
        )
        return 1

    host, _, port_str = args.bind.rpartition(":")
    if not host or not port_str:
        print(f"[ovid2 web] invalid --bind '{args.bind}' (expected HOST:PORT).")
        return 2
    try:
        port = int(port_str)
    except ValueError:
        print(f"[ovid2 web] invalid port '{port_str}'.")
        return 2

    print(f"[ovid2 web] Starting FastAPI on http://{args.bind}")
    uvicorn.run(
        "ovid2.web.app:app",
        host=host,
        port=port,
        reload=bool(args.reload),
        log_level=args.log_level,
        factory=False,
    )
    return 0
