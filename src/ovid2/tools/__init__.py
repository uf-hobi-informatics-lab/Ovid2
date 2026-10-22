"""Migration and porting utilities.

Ships tools for converting the legacy Ovid pipeline packages into Ovid2
declarative specs, and for running regression harnesses between the two
implementations.

Modules:

- :mod:`ovid2.tools.formatter_migrator` — legacy ``*Formatter`` class → YAML spec.
- :mod:`ovid2.tools.golden_diff`        — golden-file regression harness.
"""

from __future__ import annotations

__all__: list[str] = ["formatter_migrator", "golden_diff"]
