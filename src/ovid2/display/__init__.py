"""Display subsystem — event bus and pluggable renderers.

Runtime signals emitted by the pipeline (via :mod:`ovid2.core.events`) are
persisted to a JSONL file by :class:`ovid2.display.bus.JsonlEventBus`.
Renderers subscribe by tailing the file — the pipeline never imports
anything from this package (enforced by
``tests/contract/test_display_isolation.py``).

Renderers:

- :mod:`ovid2.display.renderers.plain_log`    — human-readable log stream.
- :mod:`ovid2.display.renderers.json_file`    — persistent JSONL copy.
- :mod:`ovid2.display.renderers.tui`          — Rich Live TUI (``ovid2 watch``).
- :mod:`ovid2.display.renderers.email_digest` — batch-completion email.

Full architectural description lives in ``docs/display-events.md``.
"""

from __future__ import annotations

__all__: list[str] = []
