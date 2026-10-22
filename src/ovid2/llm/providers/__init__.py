"""LLM provider registry.

``PROVIDER_TYPE_MAP`` maps a provider name to the dotted path of its
:class:`~ovid2.llm.protocol.Provider` subclass. It must stay in sync
with :data:`ovid2.config.llms._KNOWN_PROVIDERS` (contract test
``tests/contract/test_llm_catalogue_synchronised.py``).
"""

from __future__ import annotations

import importlib
from typing import Any

from ovid2.llm.protocol import Provider

PROVIDER_TYPE_MAP: dict[str, str] = {
    "openai_compatible": "ovid2.llm.providers.openai_compatible.OpenAiCompatibleProvider",
    "jev": "ovid2.llm.providers.jev.JevProvider",
}


def load_provider_class(provider: str) -> type[Provider]:
    """Import and return the provider class for ``provider``.

    Raises:
        KeyError: When the provider is not registered.
    """
    dotted = PROVIDER_TYPE_MAP[provider]
    module_name, _, cls_name = dotted.rpartition(".")
    cls = getattr(importlib.import_module(module_name), cls_name)
    if not (isinstance(cls, type) and issubclass(cls, Provider)):
        raise TypeError(f"{dotted} is not a Provider subclass.")
    return cls


def instantiate(name: str, config: dict[str, Any], secrets: dict[str, Any]) -> Provider:
    """Build a provider instance from a validated LLM config document."""
    cls = load_provider_class(str(config["provider"]))
    return cls(name, dict(config.get("args") or {}), secrets)


__all__ = ["PROVIDER_TYPE_MAP", "instantiate", "load_provider_class"]
