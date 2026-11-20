"""Domain-pack catalogue service (web tier).

Read-only view over :mod:`ovid2.packs` — the tier-neutral discovery
module that reads ``pack.yaml`` manifests and never imports pack code.
Powers ``GET /api/v1/packs``, ``GET /api/v1/packs/{id}``, the ``/packs``
page and the MCP ``list_packs`` tool.

Enabling a pack is an administrator action outside the web tier
(``OVID2_PACKS_ENABLED`` / ``$XDG_CONFIG_HOME/ovid2/packs.yaml``,
``ovid2 tools pack enable``), because an installed pack ships code that
the executor imports.
"""

from __future__ import annotations

from typing import Any

from ovid2.packs import all_packs, clear_cache, get_pack

__all__ = ["get_pack_detail", "list_packs"]


def _contributions(pack_id: str) -> dict[str, list[str]]:
    """Return the check / package ids attributed to ``pack_id``."""
    from ovid2.web.services import health_report_svc

    checks = [c["id"] for c in health_report_svc.list_checks() if c.get("pack") == pack_id]
    packages = [p["id"] for p in health_report_svc.list_packages() if p.get("pack") == pack_id]
    return {"checks": checks, "packages": packages}


def list_packs(*, refresh: bool = False) -> list[dict[str, Any]]:
    """Return every discovered pack (built-in first) with activation state.

    Args:
        refresh: Re-run discovery (picks up a newly installed / enabled pack).
    """
    if refresh:
        clear_cache()
    out: list[dict[str, Any]] = []
    for info in all_packs():
        entry = info.as_dict()
        contrib = _contributions(info.id) if info.active else {"checks": [], "packages": []}
        entry["check_count"] = len(contrib["checks"])
        entry["package_count"] = len(contrib["packages"])
        out.append(entry)
    return out


def get_pack_detail(pack_id: str) -> dict[str, Any] | None:
    """Return one pack with its contributed check / package ids, or ``None``."""
    info = get_pack(pack_id)
    if info is None:
        return None
    entry = info.as_dict()
    entry.update(_contributions(info.id) if info.active else {"checks": [], "packages": []})
    return entry


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.web.services.packs_svc is a library module.")
