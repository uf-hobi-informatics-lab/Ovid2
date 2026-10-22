"""``ovid2 service`` — install and manage the systemd unit.

Ships templates for:

- ``ovid2-scheduler.service`` (systemd unit, ``Type=simple``).
- ``ovid2.logrotate`` (daily rotation of ``/var/log/ovid2``).
- ``ovid2.tmpfiles`` (creates ``/var/run/ovid2`` on boot).

Subcommands:

- ``install`` — render templates and copy them to system locations (needs root).
- ``uninstall`` — remove the installed files.
- ``status`` / ``start`` / ``stop`` / ``restart`` / ``logs`` — thin wrappers
  around ``systemctl`` / ``journalctl`` for convenience.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

_SERVICE_NAME = "ovid2-scheduler.service"
_LOGROTATE_TARGET = Path("/etc/logrotate.d/ovid2")
_TMPFILES_TARGET = Path("/etc/tmpfiles.d/ovid2.conf")
_SYSTEMD_TARGET = Path("/etc/systemd/system") / _SERVICE_NAME


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def _load_template(name: str) -> str:
    """Return the packaged template ``resources/systemd/<name>``.

    Falls back to the on-disk ``systemd/`` directory for developer runs.
    """
    try:
        ref = resources.files("ovid2.resources.systemd") / name
        return ref.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        candidate = Path(__file__).resolve().parents[3] / "systemd" / name
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
        raise


def render_service_unit(*, user: str, ovid2_bin: str) -> str:
    """Render the systemd unit with ``user`` and ``ExecStart`` filled in."""
    template = _load_template("ovid2-scheduler.service.in")
    return template.replace("@USER@", user).replace("@OVID2_BIN@", ovid2_bin)


def render_logrotate() -> str:
    """Return the logrotate config verbatim."""
    return _load_template("ovid2.logrotate")


def render_tmpfiles(*, user: str) -> str:
    """Render the tmpfiles.d entry with the target user."""
    return _load_template("ovid2.tmpfiles").replace("@USER@", user)


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------


def _write_root_file(path: Path, contents: str, *, mode: int = 0o644) -> None:
    """Write ``contents`` to ``path`` — requires root."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    os.chmod(path, mode)


def install_service(*, user: str, ovid2_bin: str) -> list[str]:
    """Deploy the systemd unit + logrotate + tmpfiles configs.

    Returns a log of actions taken. Raises ``PermissionError`` if the
    process is not root (files land under ``/etc/`` and ``/var/``).
    """
    if os.geteuid() != 0:
        raise PermissionError(
            "'ovid2 service install' must be run as root — it writes under /etc/systemd/."
        )

    log: list[str] = []
    _write_root_file(_SYSTEMD_TARGET, render_service_unit(user=user, ovid2_bin=ovid2_bin))
    log.append(f"wrote {_SYSTEMD_TARGET}")

    _write_root_file(_LOGROTATE_TARGET, render_logrotate())
    log.append(f"wrote {_LOGROTATE_TARGET}")

    _write_root_file(_TMPFILES_TARGET, render_tmpfiles(user=user))
    log.append(f"wrote {_TMPFILES_TARGET}")

    Path("/var/log/ovid2").mkdir(parents=True, exist_ok=True)
    Path("/var/run/ovid2").mkdir(parents=True, exist_ok=True)
    log.append("created /var/log/ovid2 and /var/run/ovid2")

    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        log.append("ran 'systemctl daemon-reload'")
    if shutil.which("systemd-tmpfiles"):
        subprocess.run(["systemd-tmpfiles", "--create", str(_TMPFILES_TARGET)], check=False)
        log.append("ran 'systemd-tmpfiles --create'")
    return log


def uninstall_service() -> list[str]:
    """Remove the installed files and reload systemd. Requires root."""
    if os.geteuid() != 0:
        raise PermissionError("'ovid2 service uninstall' must be run as root.")
    log: list[str] = []
    for target in (_SYSTEMD_TARGET, _LOGROTATE_TARGET, _TMPFILES_TARGET):
        if target.exists():
            target.unlink()
            log.append(f"removed {target}")
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        log.append("ran 'systemctl daemon-reload'")
    return log


# ---------------------------------------------------------------------------
# systemctl / journalctl thin wrappers
# ---------------------------------------------------------------------------


def _systemctl(action: str) -> int:
    """Run ``systemctl <action> ovid2-scheduler.service`` and return exit."""
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        print("[ovid2 service] systemctl not found on this host.")
        return 1
    return subprocess.run([systemctl, action, _SERVICE_NAME], check=False).returncode


def _journal_logs(follow: bool) -> int:
    """Tail the journal for the ovid2-scheduler unit."""
    journalctl = shutil.which("journalctl")
    if journalctl is None:
        print("[ovid2 service] journalctl not found on this host.")
        return 1
    cmd = [journalctl, "-u", _SERVICE_NAME]
    if follow:
        cmd.append("-f")
    return subprocess.run(cmd, check=False).returncode


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``service`` subcommand and its actions."""
    parser = subparsers.add_parser("service", help="Install/manage the systemd service.")
    action = parser.add_subparsers(dest="action", metavar="ACTION", required=True)

    install = action.add_parser("install", help="Install the systemd unit (root).")
    install.add_argument(
        "--user",
        default=None,
        help="Unix user to run the daemon as (default: current $USER).",
    )
    install.add_argument(
        "--bin",
        default=None,
        help="Path to the ovid2 executable (default: resolved from PATH).",
    )
    install.set_defaults(_command="service", _entry=_entry_install)

    uninstall = action.add_parser("uninstall", help="Remove the systemd unit (root).")
    uninstall.set_defaults(_command="service", _entry=_entry_uninstall)

    for name in ("status", "start", "stop", "restart"):
        sub = action.add_parser(name, help=f"Run 'systemctl {name}' on the ovid2 service.")
        sub.set_defaults(_command="service", _entry=_make_systemctl_entry(name))

    logs = action.add_parser("logs", help="Tail journalctl for the ovid2 service.")
    logs.add_argument("-f", "--follow", action="store_true", help="Follow the journal.")
    logs.set_defaults(_command="service", _entry=_entry_logs)


def _entry_install(args: argparse.Namespace) -> int:
    """Handle ``ovid2 service install``."""
    user = args.user or getpass.getuser()
    ovid2_bin = args.bin or shutil.which("ovid2") or sys.executable
    try:
        log = install_service(user=user, ovid2_bin=ovid2_bin)
    except PermissionError as exc:
        print(f"[ovid2 service] {exc}")
        return 1
    for line in log:
        print(line)
    print("Next: 'systemctl enable --now ovid2-scheduler.service'")
    return 0


def _entry_uninstall(_args: argparse.Namespace) -> int:
    """Handle ``ovid2 service uninstall``."""
    try:
        log = uninstall_service()
    except PermissionError as exc:
        print(f"[ovid2 service] {exc}")
        return 1
    for line in log:
        print(line)
    return 0


def _make_systemctl_entry(action: str):  # type: ignore[no-untyped-def]
    """Build the entry function for a systemctl wrapper action."""
    def _entry(_args: argparse.Namespace) -> int:
        return _systemctl(action)

    _entry.__name__ = f"_entry_{action}"
    return _entry


def _entry_logs(args: argparse.Namespace) -> int:
    """Handle ``ovid2 service logs``."""
    return _journal_logs(follow=args.follow)
