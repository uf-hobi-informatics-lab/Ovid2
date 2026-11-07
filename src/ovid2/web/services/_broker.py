"""Executor-subprocess broker for credential-bearing operations.

In the harness / API / executor trust model
(the adr-harness-api-executor-trust-model design note)
the web tier (the ovid2 API) must never resolve a plaintext secret into
its own request handlers. Operations that need live credentials —
connection probes and storage browsing — are delegated to a short-lived
``ovid2 tools …`` subprocess (the executor trust domain), which reads the
secret, performs the operation, and prints a single JSON object on the
last line of stdout.

This module owns the shared subprocess-launch + JSON-parse plumbing so
``connections_svc`` and ``storage_browse_svc`` stay free of any
``resolve_secrets`` / ``load_secret`` call (enforced by
``tests/contract/test_web_no_plaintext_secret_reads.py``).

Least privilege: every call names the resources it needs as a
:class:`~ovid2.config.secret_refs.SecretScope`. The child gets an
allow-listed environment (no inherited tokens, no ``OVID2_SECRETS_FILE``)
and a private ``OVID2_SECRETS_DIR`` holding only those resources'
secrets (:func:`ovid2.config.executor_env.scoped_executor_env`).
Enforced by ``tests/contract/test_executor_subprocess_env_scoped.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from ovid2.config.executor_env import scoped_executor_env
from ovid2.config.secret_refs import SecretScope
from ovid2.web.settings import WORKER_ENV_ALLOWLIST

__all__ = ["BrokerError", "SecretScope", "run_tool_json"]

#: Wall-clock ceiling for a single brokered operation.
DEFAULT_TIMEOUT_SECONDS: float = 30.0


class BrokerError(RuntimeError):
    """Raised when the executor subprocess produced no usable JSON."""


def _ovid2_argv(tool_args: list[str]) -> list[str]:
    """Return the argv that invokes ``ovid2 tools …`` in a subprocess."""
    return [sys.executable, "-m", "ovid2", "tools", *tool_args]


def run_tool_json(
    tool_args: list[str],
    *,
    scope: SecretScope,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run an ``ovid2 tools`` subcommand and return its JSON result.

    Args:
        tool_args: Tokens after ``ovid2 tools`` (e.g.
            ``["probe-connection", "my_conn"]``).
        scope: The connections / addresses / engines whose secrets the
            subprocess may dereference. Required — there is no
            "everything" default.
        timeout: Wall-clock ceiling in seconds.

    Returns:
        The parsed last-JSON-line dict emitted by the subcommand.

    Raises:
        BrokerError: When the subprocess timed out, could not be
            launched, or emitted no parseable JSON object on stdout.
    """
    argv = _ovid2_argv(tool_args)
    label = tool_args[0] if tool_args else "tool"
    try:
        with scoped_executor_env(scope, allowlist=WORKER_ENV_ALLOWLIST, label=label) as env:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=env,
            )
    except subprocess.TimeoutExpired as exc:
        raise BrokerError(f"Operation timed out after {timeout:.0f}s: {tool_args}") from exc
    except OSError as exc:
        raise BrokerError(f"Could not launch executor subprocess: {exc}") from exc

    stdout = (completed.stdout or "").strip()
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            return candidate

    raise BrokerError(
        "Executor subprocess returned no JSON result "
        f"(exit={completed.returncode}). "
        f"stderr tail: {(completed.stderr or '')[-500:]}"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("This module is imported by the ovid2 web services.")
