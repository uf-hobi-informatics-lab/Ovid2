"""Core engine subsystem.

Contains the framework-level building blocks that do not depend on any pipeline:

* :mod:`ovid2.core.engines`      — compute engine plugins (BaseEngine ABC).
* :mod:`ovid2.core.addresses`    — I/O plugins (BaseAddress ABC): disc, Snowflake, …
* :mod:`ovid2.core.pipeline`     — the lazy ``read → format → dedupe → map → fix → write`` runner.
* :mod:`ovid2.core.isolation`    — per-invocation ``OvidInstance`` (unique compose file / network / temp dirs).
* :mod:`ovid2.core.docker`       — docker / spark-submit orchestration.
* :mod:`ovid2.core.compose`      — dynamic docker-compose YAML generator.
* :mod:`ovid2.core.loaders`      — dynamic importlib-based component loading.

Implemented over Phase 2.
"""

from __future__ import annotations

__all__: list[str] = []
