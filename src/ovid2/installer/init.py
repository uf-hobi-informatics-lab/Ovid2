"""``ovid2 init`` — bootstrap the XDG config tree.

Non-interactive by default: creates the directories, copies the sample
address configs bundled with the wheel, and writes a template
``secrets.d/snowflake.json`` (mode 0600) that the user must fill in.

An interactive Rich prompt is available with ``--interactive`` for
first-time setup on a workstation. It fills in the JSON files with
values from stdin instead of dropping placeholder templates.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
from importlib import resources
from pathlib import Path

from ovid2.config import paths as xdg_paths

# ---------------------------------------------------------------------------
# Bundled samples
# ---------------------------------------------------------------------------

_SAMPLE_ADDRESSES: dict[str, dict[str, object]] = {
    "disc_input.json": {
        "type": "disc",
        "root": str(Path.home() / "ovid2-data" / "input"),
        "format": "csv",
        "delimiter": "|",
    },
    "disc_output.json": {
        "type": "disc",
        "root": str(Path.home() / "ovid2-data" / "output"),
        "format": "csv",
        "delimiter": "|",
    },
}

_SNOWFLAKE_TEMPLATE: dict[str, str] = {
    "account": "REPLACE_ME",
    "user": "REPLACE_ME",
    "password": "REPLACE_ME",
    "role": "REPLACE_ME",
    "warehouse": "REPLACE_ME",
    "database": "REPLACE_ME",
    "schema": "REPLACE_ME",
}


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _ensure_dir(path: Path, mode: int = 0o755) -> None:
    """Create ``path`` (and parents) with the requested permission mode."""
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):  # non-POSIX / read-only fs
        os.chmod(path, mode)


def _write_json(path: Path, payload: dict[str, object], *, mode: int = 0o644) -> None:
    """Write ``payload`` as JSON, refusing to clobber an existing file."""
    if path.exists():
        return
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, mode)


def _bundle_samples_into(configs_dir: Path) -> list[Path]:
    """Materialise the built-in sample address configs into ``configs_dir``.

    Prefers files bundled in the wheel (``resources/samples/configs/``) when
    they exist, otherwise falls back to the in-module templates.
    """
    written: list[Path] = []
    try:
        sample_root = resources.files("ovid2.resources.samples.configs")
        for entry in sample_root.iterdir():
            if entry.name.endswith(".json"):
                target = configs_dir / entry.name
                if not target.exists():
                    target.write_text(entry.read_text(encoding="utf-8"), encoding="utf-8")
                    written.append(target)
    except (FileNotFoundError, ModuleNotFoundError):
        for name, payload in _SAMPLE_ADDRESSES.items():
            target = configs_dir / name
            if not target.exists():
                _write_json(target, payload)
                written.append(target)
    return written


def _prompt_snowflake(defaults: dict[str, str]) -> dict[str, str]:
    """Prompt the user for Snowflake credentials on stdin."""
    prompts: dict[str, str] = {}
    for key, default in defaults.items():
        if key == "password":
            value = getpass.getpass(f"{key} [hidden]: ")
        else:
            entered = input(f"{key} [{default}]: ").strip()
            value = entered or default
        prompts[key] = value
    return prompts


def bootstrap(*, interactive: bool = False) -> list[str]:
    """Create the XDG config tree and return a human-readable action log.

    Args:
        interactive: When ``True``, prompt for Snowflake credentials on
            stdin; when ``False``, drop a ``REPLACE_ME`` template.

    Returns:
        Ordered list of log lines describing what happened.
    """
    log: list[str] = []

    config = xdg_paths.config_dir()
    configs = xdg_paths.configs_dir()
    secrets = xdg_paths.secrets_dir()
    cache = xdg_paths.cache_dir()
    state = xdg_paths.state_dir()
    runtime = xdg_paths.runtime_dir()

    for label, path, mode in (
        ("config", config, 0o755),
        ("configs", configs, 0o755),
        ("secrets", secrets, 0o700),
        ("cache", cache, 0o755),
        ("state", state, 0o755),
        ("runtime", runtime, 0o700),
    ):
        existed = path.is_dir()
        _ensure_dir(path, mode=mode)
        log.append(f"{'kept' if existed else 'created'} {label} dir: {path}")

    written = _bundle_samples_into(configs)
    for entry in written:
        log.append(f"wrote sample config: {entry}")
    if not written:
        log.append("sample configs already present (skipping)")

    snowflake_path = secrets / "snowflake.json"
    if not snowflake_path.exists():
        payload = _prompt_snowflake(_SNOWFLAKE_TEMPLATE) if interactive else _SNOWFLAKE_TEMPLATE
        _write_json(snowflake_path, dict(payload), mode=0o600)
        log.append(f"wrote {snowflake_path} (mode 0600)")
    else:
        log.append(f"kept existing {snowflake_path}")

    return log


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``init`` subcommand."""
    parser = subparsers.add_parser(
        "init",
        help="Bootstrap XDG config directories and sample configs.",
        description=(
            "Creates $XDG_CONFIG_HOME/ovid2/{configs,secrets.d}, drops "
            "sample address configs, and writes a Snowflake secret template."
        ),
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt for Snowflake credentials on stdin instead of writing a template.",
    )
    parser.set_defaults(_command="init", _entry=_entry)


def _entry(args: argparse.Namespace) -> int:
    """Run bootstrap and print the log."""
    log = bootstrap(interactive=args.interactive)
    for line in log:
        print(line)
    return 0
