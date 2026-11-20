"""CDM valuesets registered as vocabulary handles.

Introduced by phase C1 for PCORnet CDM v7.0; generalised for domain
packs so every registered YAML CDM (core or pack) gets the same
treatment. Reads every
``<cdm>/valuesets/<table>.<field>.yaml`` (e.g.
``resources/cdms/pcornet_7_0/valuesets/…``) and
registers a matching :class:`~ovid2.semantic.handle.VocabularyHandle`
with an ``inline_items`` source leg plus a
:class:`~ovid2.semantic.registry.ColumnBinding` under
``('pcornet_7_0', <table>, <FIELD>)``.

Vocabularies are named dotted-lowercase — e.g.
``pcornet_7_0.demographic.sex`` — so a single semantic registry cleanly
carries valuesets from multiple CDMs without name collisions.

The registration is idempotent: import errors, malformed YAMLs, and
already-registered handles are logged and skipped rather than raised,
so a partial vocabulary tree never breaks CDM boot.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from ovid2.cdm.yaml_loader import iter_valueset_docs
from ovid2.semantic.handle import VocabularyHandle
from ovid2.semantic.registry import (
    ColumnBinding,
    bind_column,
    register,
)

logger = logging.getLogger(__name__)

_CDM_NAME = "pcornet_7_0"


def _register_all_cdms() -> None:
    """Register valuesets for PCORnet and every other registered YAML CDM."""
    from ovid2.cdm.registry import registered_cdms

    names = [_CDM_NAME] + [n for n in registered_cdms() if n != _CDM_NAME]
    for name in names:
        _register_valuesets(name)


def _register_valuesets(cdm_name: str = _CDM_NAME) -> None:
    """Walk one CDM's YAML tree and register every valueset."""
    count = 0
    for table, field, payload in iter_valueset_docs(cdm_name):
        try:
            handle = _build_handle(payload, cdm_name)
        except (ValueError, KeyError) as exc:
            logger.warning(
                "Skipping %s valueset %s.%s: %s",
                cdm_name,
                table,
                field,
                exc,
            )
            continue
        # Already registered — safe in reload/test scenarios.
        with contextlib.suppress(ValueError):
            register(handle)
        # Bind the vocabulary to the CDM column so `ovid2 semantic bindings`
        # and future strict-mode checks can find it.
        role_prefix = "pcornet" if cdm_name == _CDM_NAME else cdm_name
        binding = ColumnBinding(
            cdm=cdm_name,
            table=table,
            column=field.upper(),
            vocabulary=handle.name,
            role=f"{role_prefix}_{table}_{field.lower()}",
        )
        # Duplicate binding — safe in reload/test scenarios.
        with contextlib.suppress(ValueError):
            bind_column(binding)
        count += 1
    logger.debug("Registered %d %s valueset(s).", count, cdm_name)


def _build_handle(payload: dict[str, Any], cdm_name: str = _CDM_NAME) -> VocabularyHandle:
    """Convert one valueset YAML payload into a :class:`VocabularyHandle`."""
    name = payload["name"]
    items_raw = payload.get("items") or []
    items: list[tuple[str, str | None]] = []
    for item in items_raw:
        code = str(item.get("code", ""))
        label = item.get("label")
        items.append((code, str(label) if label is not None else None))
    return VocabularyHandle(
        name=str(name),
        version=str(payload.get("version") or ""),
        description=str(payload.get("description") or ""),
        code_column=str(payload.get("code_column") or "code"),
        description_column=(
            str(payload["description_column"]) if payload.get("description_column") else None
        ),
        inline_items=tuple(items),
        tags=("clinical", "pcornet") if cdm_name == _CDM_NAME else ("valueset", cdm_name),
    )


_register_all_cdms()
