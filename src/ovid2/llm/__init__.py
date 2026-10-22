"""Executor-tier LLM runtime (L1): providers, dual-system router, CLI.

This package is the **only** place that dereferences an LLM API key. It
runs in a short-lived executor subprocess (``python -m ovid2.llm …`` /
``ovid2 tools llm-chat``) spawned by the web tier, mirroring the
connection-probe broker described in
the adr-harness-api-executor-trust-model design note.

Layout:

- :mod:`ovid2.llm.protocol` — ``Provider`` ABC, ``Decision`` record and
  the JSON-lines event vocabulary shared with the web tier.
- :mod:`ovid2.llm.providers` — one module per wire protocol
  (``openai_compatible`` for any OpenAI-compatible model, ``jev`` stub for the
  upcoming System 1 model) plus ``PROVIDER_TYPE_MAP``.
- :mod:`ovid2.llm.router` — System 1 first, general fallback.
- :mod:`ovid2.llm.cli` — ``chat`` / ``probe`` entry points.

The web tier (``ovid2.web``) and the agent tier (``ovid2.mcp``) must
never import this package (``tests/contract/test_llm_tier_isolation.py``).
"""

from __future__ import annotations
