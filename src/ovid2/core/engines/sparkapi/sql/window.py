"""Portable ``pyspark.sql.window``."""

from __future__ import annotations

from ovid2.core.engines.dsl import Window
from ovid2.core.engines.sparkapi._placeholders import WindowSpec

__all__ = ["Window", "WindowSpec"]
