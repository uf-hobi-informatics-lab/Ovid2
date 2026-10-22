"""Contract: the OpenAPI schema is stable across commits.

FastAPI auto-generates ``/openapi.json``. That schema is the source of
truth the MCP server derives its tool definitions from, so any
inadvertent change (e.g. renaming a query parameter, dropping a field)
is a breaking change for downstream agents.

The snapshot lives at ``tests/contract/data/openapi.snapshot.json``.

To regenerate after an intentional schema change:

.. code-block:: console

   pytest tests/contract/test_web_openapi_stable.py --snapshot-update

We do not depend on ``syrupy`` — the update mode is triggered by the
``OVID2_UPDATE_OPENAPI_SNAPSHOT=1`` environment variable so the test
stays vanilla-pytest.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

_SNAPSHOT: Path = Path(__file__).parent / "data" / "openapi.snapshot.json"


def _load_app() -> object:
    """Import the FastAPI app; skip the test when web extras are missing."""
    fastapi = pytest.importorskip("fastapi")  # noqa: F841 — used only for skip semantics
    from ovid2.web.app import app

    return app


#: Components FastAPI generates itself (422 bodies). Their shape follows
#: the installed FastAPI version (0.12x added ``input``/``ctx``), not our
#: API, so they are excluded from the drift check.
_FRAMEWORK_COMPONENTS: frozenset[str] = frozenset({"ValidationError", "HTTPValidationError"})


def _without_framework_components(schema: dict[str, object]) -> dict[str, object]:
    """Return ``schema`` minus FastAPI-owned component schemas."""
    components = dict(schema.get("components") or {})  # type: ignore[call-overload]
    schemas = {
        k: v
        for k, v in dict(components.get("schemas") or {}).items()
        if k not in _FRAMEWORK_COMPONENTS
    }
    return {**schema, "components": {**components, "schemas": schemas}}


@pytest.mark.contract
def test_openapi_snapshot_matches() -> None:
    """The generated OpenAPI must match the committed snapshot."""
    app = _load_app()
    schema = app.openapi()  # type: ignore[attr-defined]

    if os.environ.get("OVID2_UPDATE_OPENAPI_SNAPSHOT") == "1":
        _SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        _SNAPSHOT.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip(f"Snapshot updated at {_SNAPSHOT}")

    if not _SNAPSHOT.is_file():
        pytest.skip(
            f"Snapshot missing at {_SNAPSHOT}. "
            "Run with OVID2_UPDATE_OPENAPI_SNAPSHOT=1 to create it."
        )

    expected = _without_framework_components(json.loads(_SNAPSHOT.read_text(encoding="utf-8")))
    actual = _without_framework_components(json.loads(json.dumps(schema, sort_keys=True)))
    assert actual == expected, (
        "OpenAPI schema drifted. If intentional, regenerate with OVID2_UPDATE_OPENAPI_SNAPSHOT=1."
    )
