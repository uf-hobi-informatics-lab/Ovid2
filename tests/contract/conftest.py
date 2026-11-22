"""Contract-suite conftest: every test under ``tests/contract/`` is a contract.

CI selects the gate with ``pytest -m contract``. A contract file that
forgot ``@pytest.mark.contract`` / ``pytestmark`` silently fell out of that
gate (45 tests in 11 files did, until 2026-11-20). This hook marks every
collected item under this directory, so the directory *is* the marker.
``test_contract_suite_self_marked.py`` pins the behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Add ``pytest.mark.contract`` to every item collected from this directory."""
    del config
    for item in items:
        if _HERE in Path(str(item.fspath)).resolve().parents:
            item.add_marker(pytest.mark.contract)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("pytest conftest; not executable.")
