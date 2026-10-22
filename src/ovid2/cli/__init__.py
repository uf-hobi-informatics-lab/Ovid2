"""Ovid2 command-line interface.

Subcommands are wired here and dispatched from :func:`ovid2.cli.__main__.main`.

The dispatcher is intentionally thin — every subcommand lives in its own module
inside :mod:`ovid2.cli` and is imported lazily so ``ovid2 --help`` stays fast.
"""

from __future__ import annotations

__all__: list[str] = []
