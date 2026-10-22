"""Allow ``python -m ovid2`` to launch the CLI."""

from __future__ import annotations

from ovid2.cli.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
