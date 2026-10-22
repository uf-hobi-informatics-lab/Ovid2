"""JEV — System 1 decision provider (placeholder until JEV ships).

The connection can be configured today (``base_url`` / ``model`` /
``confidence_threshold`` / optional ``api_key``). Both :meth:`probe`
and :meth:`decide` report ``not_implemented``; the router treats that
as "System 1 abstained" and falls back to the general LLM.

When the JEV API spec is available, implement :meth:`decide` to call
the JEV endpoint and return a :class:`~ovid2.llm.protocol.Decision`.
Nothing else in the stack needs to change.
"""

from __future__ import annotations

from ovid2.llm.protocol import ChatMessage, Decision, ProbeResult, Provider


class JevProvider(Provider):
    """System 1 (JEV) decision model — not yet available."""

    provider_name = "jev"
    roles = ("system1",)

    def probe(self) -> ProbeResult:
        """Report that the JEV client is not implemented yet."""
        return ProbeResult(
            False,
            "not_implemented",
            "JEV provider is not available yet; the router will fall back to the general LLM.",
            {"base_url": str(self.args.get("base_url") or "")},
        )

    def decide(self, messages: list[ChatMessage]) -> Decision:
        """Raise until the JEV wire protocol is implemented."""
        raise NotImplementedError("JEV provider is not available yet.")


__all__ = ["JevProvider"]
