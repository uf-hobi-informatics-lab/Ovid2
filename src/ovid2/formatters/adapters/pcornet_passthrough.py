"""Deprecated alias for :mod:`source_passthrough`.

.. deprecated:: P10

    ``pcornet_passthrough`` was a misnomer — the adapter contains no
    PCORnet-specific logic and every non-OMOP pipeline uses it regardless
    of target CDM. Import
    :class:`~ovid2.formatters.adapters.source_passthrough.SourcePassthroughAdapter`
    directly, or set ``adapter: source_passthrough`` in your spec.

    The class below is a re-exported subclass, kept so existing spec files
    that name ``pcornet_passthrough`` in their ``adapter:`` field continue
    to load without modification.
"""

from __future__ import annotations

from ovid2.formatters.adapters.source_passthrough import SourcePassthroughAdapter


class PcornetPassthroughAdapter(SourcePassthroughAdapter):
    """Deprecated alias — use :class:`SourcePassthroughAdapter` instead."""


__all__ = ["PcornetPassthroughAdapter"]
