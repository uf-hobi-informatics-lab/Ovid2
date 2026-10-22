"""``ovid2 doctor`` — environment diagnostics.

Runs a series of read-only checks and prints a formatted report. Every check
returns a :class:`CheckResult`; the overall exit code is non-zero if any
check has severity ``error``.

Checks performed:

- Python version and importability of core deps (``pyspark`` optional).
- XDG directories exist and are writable.
- Presence of ``$XDG_CONFIG_HOME/ovid2/configs/*.json`` address configs.
- Permissions of ``secrets.d/*.json`` (must be 0600).
- ``docker`` on ``$PATH`` (optional — required only for host mode).
- Reachability of the Ovid2 image (``docker image inspect``) — optional.
- Systemd unit installed (``ovid2-scheduler.service``) — optional.
- Materialised CDM CSV (``pcornet_7_0/pcornet_cdm_v70.csv``) — warning only.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ovid2.config import paths as xdg_paths
from ovid2.config.settings import PCORNET_CDM_CSV_PATH

# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

_SEVERITY_ORDER = {"ok": 0, "warning": 1, "error": 2}


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single diagnostic check."""

    name: str
    severity: str  # "ok" | "warning" | "error"
    message: str
    hint: str | None = None


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_python_version() -> CheckResult:
    """Verify Python interpreter is 3.11+."""
    version = sys.version_info
    if version < (3, 11):
        return CheckResult(
            name="python",
            severity="error",
            message=f"Python {version.major}.{version.minor} is too old.",
            hint="Install Python 3.11 or newer.",
        )
    return CheckResult(
        name="python",
        severity="ok",
        message=f"Python {version.major}.{version.minor}.{version.micro}",
    )


def _check_optional_import(module: str) -> CheckResult:
    """Report whether ``module`` can be imported.

    Uses :func:`importlib.util.find_spec`; for nested paths like
    ``snowflake.connector`` we first check the top-level package to avoid
    raising when a parent is missing.
    """
    top_level = module.split(".", 1)[0]
    try:
        top_spec = importlib.util.find_spec(top_level)
    except (ImportError, ValueError):
        top_spec = None
    if top_spec is None:
        return CheckResult(
            name=f"import:{module}",
            severity="warning",
            message=f"{module!r} not importable.",
            hint=f"pip install {top_level}",
        )
    if top_level == module:
        return CheckResult(name=f"import:{module}", severity="ok", message=f"{module} available")

    try:
        nested_spec = importlib.util.find_spec(module)
    except (ImportError, ValueError, ModuleNotFoundError):
        nested_spec = None
    if nested_spec is None:
        return CheckResult(
            name=f"import:{module}",
            severity="warning",
            message=f"{module!r} not importable (parent {top_level!r} present).",
        )
    return CheckResult(name=f"import:{module}", severity="ok", message=f"{module} available")


def _check_writable_dir(label: str, path: Path, *, create: bool = True) -> CheckResult:
    """Ensure ``path`` exists (creating if requested) and is writable."""
    try:
        if create:
            path.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            return CheckResult(
                name=f"dir:{label}",
                severity="error",
                message=f"{path} does not exist.",
            )
        if not os.access(path, os.W_OK):
            return CheckResult(
                name=f"dir:{label}",
                severity="error",
                message=f"{path} is not writable by uid {os.getuid()}.",
            )
    except OSError as exc:
        return CheckResult(
            name=f"dir:{label}",
            severity="error",
            message=f"Cannot access {path}: {exc}",
        )
    return CheckResult(name=f"dir:{label}", severity="ok", message=str(path))


def _check_secrets_permissions() -> list[CheckResult]:
    """Verify every ``secrets.d/*.json`` file is mode 0600."""
    secrets = xdg_paths.secrets_dir()
    if not secrets.is_dir():
        return [
            CheckResult(
                name="secrets",
                severity="warning",
                message=f"{secrets} does not exist yet.",
                hint="Run 'ovid2 init' to lay out the config tree.",
            )
        ]

    results: list[CheckResult] = []
    for entry in sorted(secrets.glob("*.json")):
        mode = entry.stat().st_mode & 0o777
        if mode & (stat.S_IROTH | stat.S_IWOTH | stat.S_IRGRP | stat.S_IWGRP):
            results.append(
                CheckResult(
                    name=f"secret:{entry.name}",
                    severity="error",
                    message=f"{entry} has mode {oct(mode)} (must be 0600).",
                    hint=f"chmod 600 {entry}",
                )
            )
        else:
            results.append(
                CheckResult(name=f"secret:{entry.name}", severity="ok", message="mode 0600")
            )
    if not results:
        results.append(
            CheckResult(
                name="secrets",
                severity="warning",
                message="No secrets configured.",
                hint="Add JSON files under secrets.d/ (mode 0600).",
            )
        )
    return results


def _check_address_configs() -> CheckResult:
    """Verify address configs exist under ``configs/``."""
    configs = xdg_paths.configs_dir()
    if not configs.is_dir():
        return CheckResult(
            name="configs",
            severity="warning",
            message=f"{configs} does not exist.",
            hint="Copy sample configs from the repository or run 'ovid2 init'.",
        )
    json_files = list(configs.glob("*.json"))
    if not json_files:
        return CheckResult(
            name="configs",
            severity="warning",
            message=f"{configs} is empty.",
            hint="Add address JSON files (see docs/installer.md).",
        )
    return CheckResult(
        name="configs",
        severity="ok",
        message=f"{len(json_files)} address config(s) present.",
    )


def _check_docker() -> CheckResult:
    """Ensure ``docker`` is on ``$PATH`` and daemon is reachable."""
    docker = shutil.which("docker")
    if docker is None:
        return CheckResult(
            name="docker",
            severity="warning",
            message="`docker` not on PATH.",
            hint="Install Docker or ensure PATH includes /usr/bin.",
        )
    try:
        completed = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return CheckResult(
            name="docker",
            severity="warning",
            message=f"Docker CLI present but daemon unreachable: {exc}",
        )
    if completed.returncode != 0:
        return CheckResult(
            name="docker",
            severity="warning",
            message="Docker daemon not reachable.",
            hint="Start Docker or add the current user to the 'docker' group.",
        )
    return CheckResult(
        name="docker",
        severity="ok",
        message=f"Docker daemon reachable (server {completed.stdout.strip()}).",
    )


def _check_docker_image(image_ref: str) -> CheckResult:
    """Verify the configured Ovid2 image is present locally."""
    docker = shutil.which("docker")
    if docker is None:
        return CheckResult(
            name="image",
            severity="warning",
            message="`docker` not on PATH — skipping image check.",
        )
    try:
        completed = subprocess.run(
            [docker, "image", "inspect", image_ref],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return CheckResult(
            name="image",
            severity="warning",
            message=f"Cannot inspect image {image_ref}: {exc}",
        )
    if completed.returncode != 0:
        return CheckResult(
            name="image",
            severity="warning",
            message=f"Docker image {image_ref!r} not present locally.",
            hint=f"Run 'ovid2 image build --tag {image_ref}'.",
        )
    return CheckResult(name="image", severity="ok", message=f"Image {image_ref} present.")


def _check_systemd() -> CheckResult:
    """Check whether the ovid2-scheduler service unit is installed."""
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return CheckResult(
            name="systemd",
            severity="ok",
            message="systemctl not present (non-systemd host).",
        )
    try:
        completed = subprocess.run(
            [systemctl, "list-unit-files", "ovid2-scheduler.service", "--no-legend"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return CheckResult(
            name="systemd",
            severity="warning",
            message=f"systemctl invocation failed: {exc}",
        )
    if "ovid2-scheduler.service" not in completed.stdout:
        return CheckResult(
            name="systemd",
            severity="warning",
            message="ovid2-scheduler.service not installed.",
            hint="Run 'ovid2 service install' to deploy the systemd unit.",
        )
    return CheckResult(
        name="systemd",
        severity="ok",
        message="ovid2-scheduler.service is registered.",
    )


def _check_cdm_csv() -> CheckResult:
    """Check whether the PCORnet CDM CSV is present."""
    if PCORNET_CDM_CSV_PATH.is_file():
        return CheckResult(
            name="cdm",
            severity="ok",
            message=f"PCORnet CDM CSV present at {PCORNET_CDM_CSV_PATH}.",
        )
    return CheckResult(
        name="cdm",
        severity="warning",
        message=f"PCORnet CDM CSV missing at {PCORNET_CDM_CSV_PATH}.",
        hint="Formatter output-column validation will be skipped without this file.",
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_all_checks(*, image_ref: str | None = None) -> list[CheckResult]:
    """Run every diagnostic and return the collected results.

    Args:
        image_ref: Optional Docker image tag to look up (defaults to
            ``OVID2_DOCKER_IMAGE`` from settings when ``None``).

    Returns:
        List of :class:`CheckResult`, one per finding.
    """
    from ovid2.config.settings import DOCKER_IMAGE

    results: list[CheckResult] = []
    results.append(_check_python_version())
    for module in ("yaml", "jsonschema", "rich", "snowflake.connector"):
        results.append(_check_optional_import(module))
    results.append(_check_optional_import("pyspark"))

    results.append(_check_writable_dir("config", xdg_paths.config_dir()))
    results.append(_check_writable_dir("cache", xdg_paths.cache_dir()))
    results.append(_check_writable_dir("state", xdg_paths.state_dir()))
    results.append(_check_writable_dir("runtime", xdg_paths.runtime_dir()))

    results.append(_check_address_configs())
    results.extend(_check_secrets_permissions())
    results.append(_check_docker())
    results.append(_check_docker_image(image_ref or DOCKER_IMAGE))
    results.append(_check_systemd())
    results.append(_check_cdm_csv())
    return results


def format_report(results: list[CheckResult]) -> str:
    """Render ``results`` as a human-readable string."""
    max_name = max((len(r.name) for r in results), default=10)
    lines: list[str] = ["Ovid2 doctor report", "=" * 30]
    for r in results:
        badge = {"ok": "OK   ", "warning": "WARN ", "error": "FAIL "}.get(r.severity, "?    ")
        lines.append(f"  [{badge}] {r.name:<{max_name}}  {r.message}")
        if r.hint and r.severity != "ok":
            lines.append(f"          hint: {r.hint}")
    return "\n".join(lines)


def _highest_severity(results: list[CheckResult]) -> str:
    """Return the highest-severity level present in ``results``."""
    return max(
        (r.severity for r in results),
        key=lambda s: _SEVERITY_ORDER.get(s, 0),
        default="ok",
    )


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``doctor`` subcommand."""
    parser = subparsers.add_parser(
        "doctor",
        help="Diagnose environment (docker, jars, snowflake, permissions).",
    )
    parser.add_argument(
        "--image",
        default=None,
        help="Docker image tag to look up (defaults to $OVID2_DOCKER_IMAGE).",
    )
    parser.set_defaults(_command="doctor", _entry=_entry)


def _entry(args: argparse.Namespace) -> int:
    """Run every check, print the report, exit non-zero on failures."""
    results = run_all_checks(image_ref=args.image)
    print(format_report(results))
    worst = _highest_severity(results)
    return 1 if worst == "error" else 0
