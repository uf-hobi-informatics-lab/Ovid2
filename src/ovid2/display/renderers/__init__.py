"""Display renderers — subscribers of the event bus.

Each renderer converts events into a specific output medium (terminal,
log file, JSON stream, email) and is completely decoupled from pipeline
execution. Renderers can be attached as :class:`~ovid2.core.events.EventBus`
implementations (for in-process fan-out) or run as separate processes
that tail the JSONL event log.
"""

from __future__ import annotations

__all__: list[str] = []
