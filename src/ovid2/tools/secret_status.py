"""``ovid2 tools secret-status`` — value-free status of one secret (executor tier).

The web install wizard needs to know whether ``secrets.d/<name>.json`` is
filled in (not a ``REPLACE_ME`` template) without the web process ever
reading the plaintext. It brokers this subcommand through
:func:`ovid2.web.services._broker.run_tool_json` with a
:class:`~ovid2.config.secret_refs.SecretScope` naming only that secret;
the subcommand prints one JSON object with key names, file mode and the
list of unfilled required keys — never a value.
"""

from __future__ import annotations

import argparse
import json

from ovid2.config.secrets import secret_status

__all__ = ["register"]


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``secret-status`` on the ``ovid2 tools`` parser."""
    p = subparsers.add_parser(
        "secret-status",
        help="Report whether a secret exists / is filled in (key names only, no values).",
        description=(
            "Loads secrets.d/<name>.json in the executor and prints one JSON "
            "object: exists, keys, mode_0600, filled, missing. Values are never printed."
        ),
    )
    p.add_argument("--name", required=True, help="Secret name (file stem under secrets.d/).")
    p.add_argument(
        "--require",
        default="",
        help="Comma-separated keys that must hold a non-placeholder value.",
    )
    p.set_defaults(_entry=_entry, _tool="secret-status")


def _entry(args: argparse.Namespace) -> int:
    """Print the value-free status JSON and return 0 (status is data, not exit code)."""
    required = [k.strip() for k in str(args.require).split(",") if k.strip()]
    try:
        report = secret_status(str(args.name), required)
    except ValueError as exc:  # invalid secret name
        print(json.dumps({"name": args.name, "exists": False, "error": str(exc)}))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run via `ovid2 tools secret-status`.")
