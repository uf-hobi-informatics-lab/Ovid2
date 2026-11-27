"""Legacy runner / fix stage stop at a cancel the engine reported as an error."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from ovid2.core import cancellation
from ovid2.legacy import fix_stage

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    cancellation.reset()
    yield
    cancellation.reset()


def test_raise_if_cancelled_chains_cause() -> None:
    cancellation.raise_if_cancelled()  # not cancelled: no-op
    cancellation.request_cancel()
    err = RuntimeError("Query interrupted")
    with pytest.raises(KeyboardInterrupt) as info:
        cancellation.raise_if_cancelled(err)
    assert info.value.__cause__ is err


def test_fix_stage_stops_after_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []

    def _fixes(_spark: Any, *, table: str, **_kw: Any) -> None:
        ran.append(table)
        cancellation.request_cancel()
        raise RuntimeError("Query interrupted")

    monkeypatch.setattr(fix_stage, "run_table_fixes", _fixes)
    monkeypatch.setattr(fix_stage, "run_multi_table_fixes", lambda *_a, **_k: None)
    with pytest.raises(KeyboardInterrupt):
        fix_stage.run_fix_stage(
            None, partner="demo", tables=["a", "b"], work_root="/nonexistent", folder_label="x"
        )
    assert ran == ["a"]
