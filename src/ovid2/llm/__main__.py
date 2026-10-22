"""``python -m ovid2.llm`` — fast executor entry point (see :mod:`ovid2.llm.cli`)."""

from __future__ import annotations

from ovid2.llm.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
