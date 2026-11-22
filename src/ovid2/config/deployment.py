"""Deployment mode: ``dev`` vs ``production`` (web-safe, no heavy imports).

Secure defaults hinge on one question — *is this a developer sandbox?* —
answered by the ``OVID2_DEPLOYMENT`` environment variable:

* ``dev`` — set by ``ovid2 dev up`` for its private instances. Auth is off
  by default and secrets-scope mode defaults to ``warn``.
* ``production`` (or **unset / anything else**) — auth is **on** by default,
  scope mode defaults to ``enforce``, and an unreadable ``settings.json``
  fails closed (auth stays on).

Unset means production on purpose: a forgotten variable must never open
the panel. ADR: the adr-secure-defaults design note.
"""

from __future__ import annotations

import ipaddress
import os
from typing import Literal

__all__ = [
    "DEPLOYMENT_ENV",
    "DeploymentMode",
    "deployment_mode",
    "is_dev",
    "is_loopback_host",
]

#: Environment variable selecting the deployment mode.
DEPLOYMENT_ENV: str = "OVID2_DEPLOYMENT"

DeploymentMode = Literal["dev", "production"]


def deployment_mode() -> DeploymentMode:
    """Return the active deployment mode (``production`` unless explicitly ``dev``)."""
    raw = os.environ.get(DEPLOYMENT_ENV, "").strip().lower()
    return "dev" if raw == "dev" else "production"


def is_dev() -> bool:
    """Return ``True`` only for an explicit ``OVID2_DEPLOYMENT=dev``."""
    return deployment_mode() == "dev"


def is_loopback_host(host: str) -> bool:
    """Return whether ``host`` binds only the local machine.

    ``localhost`` and any loopback IP (``127.0.0.0/8``, ``::1``) count;
    ``0.0.0.0`` / ``::`` / LAN addresses / hostnames do not.
    """
    value = host.strip().strip("[]")
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Imported by ovid2 config / web / cli.")
