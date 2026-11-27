"""Algorithms ⟂ compute ⟂ storage: the single algorithm entry point.

:class:`~ovid2.algorithms.registry.AlgorithmRegistry` is to algorithms what
:mod:`ovid2.drivers.registry` is to storage and
:mod:`ovid2.core.engines.registry` is to compute. Import the facade lazily
from executor code::

    from ovid2.algorithms.registry import registry

    fixer = registry.get("fixer", "patid_orphans")

Importing this package loads nothing but the protocol.
"""

from __future__ import annotations

from ovid2.algorithms.protocol import Algorithm, AlgorithmKind

__all__ = ["Algorithm", "AlgorithmKind"]
