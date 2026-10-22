"""Installer subsystem.

Implements the ``ovid2 init``, ``ovid2 doctor``, ``ovid2 image``, and
``ovid2 service`` subcommands. Follows XDG Base Directory conventions:

* User config:  ``$XDG_CONFIG_HOME/ovid2/`` (default ``~/.config/ovid2/``)
* Secrets:      ``$XDG_CONFIG_HOME/ovid2/secrets.d/*.json`` (mode ``0600``)
* Cache:        ``$XDG_CACHE_HOME/ovid2/``
* State:        ``$XDG_STATE_HOME/ovid2/``
* Runtime:      ``$XDG_RUNTIME_DIR/ovid2/``
* System:       ``/etc/ovid2/``, ``/var/log/ovid2/``, ``/var/run/ovid2/``

Phase 6 modules:

- :mod:`ovid2.installer.doctor` — ``ovid2 doctor``
- :mod:`ovid2.installer.init` — ``ovid2 init``
- :mod:`ovid2.installer.image` — ``ovid2 image``
- :mod:`ovid2.installer.service` — ``ovid2 service``
"""

from __future__ import annotations

__all__: list[str] = ["doctor", "image", "init", "service"]
