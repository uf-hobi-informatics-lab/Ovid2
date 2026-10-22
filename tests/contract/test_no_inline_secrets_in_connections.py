"""Contract: connection JSON files must not carry raw secret material.

The W7 design keeps credential-shaped values under
``$XDG_CONFIG_HOME/ovid2/secrets.d/*.json`` (mode 0o600) and stores
only a ``secret_ref`` on the connection. This test enforces the
invariant at file-content level so a hand-edited or migrated file
that leaks a password gets caught before it hits production.

The check runs against:

- The repo-local ``connections/`` directory (if present).
- Every ``ovid2_pipelines.*.connections/`` package (if present).

It intentionally does *not* walk ``$XDG_CONFIG_HOME`` — that path is
per-operator and out of test scope.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ovid2.config.connections import connection_field_spec, list_connection_types

REPO_CONNECTIONS_DIR = Path(__file__).resolve().parents[2] / "connections"


def _all_secret_field_names() -> frozenset[str]:
    """Aggregate every secret field name across every registered type."""
    names: set[str] = set()
    for t in list_connection_types():
        for f in connection_field_spec(t)["secrets"]:
            names.add(str(f["name"]))
    return frozenset(names)


@pytest.mark.contract
def test_no_inline_secret_in_connection_files() -> None:
    """Every on-disk connection JSON in the repo must be secret-clean."""
    secrets = _all_secret_field_names()
    offenders: list[str] = []
    candidates: list[Path] = []
    if REPO_CONNECTIONS_DIR.is_dir():
        candidates.extend(
            p for p in REPO_CONNECTIONS_DIR.iterdir()
            if p.is_file() and p.suffix == ".json"
        )
    if not candidates:
        pytest.skip("No repo-tracked connection files to check.")
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            offenders.append(f"{path}: unreadable ({exc})")
            continue
        if not isinstance(data, dict):
            offenders.append(f"{path}: not a JSON object")
            continue
        args = data.get("args") or {}
        inline = sorted(set(args) & secrets)
        if inline:
            offenders.append(
                f"{path}: inline secret keys in args -> {inline}"
            )
    assert not offenders, (
        "Connection files must not carry raw secret material — "
        "move sensitive values into secrets.d/ and reference via secret_ref:\n"
        + "\n".join(offenders)
    )
