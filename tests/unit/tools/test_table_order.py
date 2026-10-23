"""Unit tests for :mod:`ovid2.tools.table_order` (largest-first copy scheduling)."""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from ovid2.tools.table_order import (
    copy_order_policy,
    format_size,
    order_largest_first,
    sort_by_size,
)
from ovid2.tools.table_specs import TableSpec


def _specs(*names: str) -> list[TableSpec]:
    return [TableSpec(table_name=n, file_name=n) for n in names]


def _names(specs: list[TableSpec]) -> list[str]:
    return [s.table_name for s in specs]


@pytest.mark.unit
def test_sort_by_size_descending_unknown_last_stable() -> None:
    specs = _specs("demographic", "provider", "lab_result_cm", "obs_clin", "harvest", "x")
    sizes = {"demographic": 50, "lab_result_cm": 900, "obs_clin": 400, "provider": 50}
    assert _names(sort_by_size(specs, sizes)) == [
        "lab_result_cm",
        "obs_clin",
        "demographic",  # tie with provider: input order kept
        "provider",
        "harvest",  # unknown sizes keep input order, after sized
        "x",
    ]


@pytest.mark.unit
def test_order_largest_first_uses_source_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OVID2_COPY_ORDER", raising=False)
    driver = mock.MagicMock()
    driver.table_sizes.return_value = {"b": 10, "c": 30}
    ordered, sizes = order_largest_first(_specs("a", "b", "c"), driver, pipeline="p", batch_id="1")
    assert _names(ordered) == ["c", "b", "a"]
    assert sizes == {"b": 10, "c": 30}
    driver.table_sizes.assert_called_once_with(["a", "b", "c"], pipeline="p", batch_id="1")


@pytest.mark.unit
def test_order_as_given_policy_skips_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OVID2_COPY_ORDER", "as_given")
    driver = mock.MagicMock()
    ordered, sizes = order_largest_first(_specs("a", "b"), driver, pipeline="p", batch_id="1")
    assert _names(ordered) == ["a", "b"] and sizes == {}
    driver.table_sizes.assert_not_called()


@pytest.mark.unit
def test_unknown_policy_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OVID2_COPY_ORDER", "bogus")
    assert copy_order_policy() == "largest_first"


@pytest.mark.unit
@pytest.mark.parametrize(
    "result",
    [RuntimeError("api down"), {}, mock.MagicMock(), {"a": "big"}],
)
def test_size_lookup_failure_keeps_order(result: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OVID2_COPY_ORDER", raising=False)
    driver = mock.MagicMock()
    if isinstance(result, Exception):
        driver.table_sizes.side_effect = result
    else:
        driver.table_sizes.return_value = result
    ordered, sizes = order_largest_first(_specs("a", "b"), driver, pipeline="p", batch_id="1")
    assert _names(ordered) == ["a", "b"] and sizes == {}


@pytest.mark.unit
def test_single_table_skips_lookup() -> None:
    driver = mock.MagicMock()
    order_largest_first(_specs("a"), driver, pipeline="p", batch_id="1")
    driver.table_sizes.assert_not_called()


@pytest.mark.unit
def test_format_size() -> None:
    assert format_size(512) == "512 B"
    assert format_size(2048) == "2.0 KB"
    assert format_size(5 * 1024**3) == "5.0 GB"


@pytest.mark.unit
def test_largest_first_reduces_makespan_on_observed_run() -> None:
    """Replay the 2026-10-23 acme→sandbox durations through an 8-slot pool.

    Durations (seconds) are the per-table times from that run;
    ``lab_result_cm`` / ``obs_clin`` were still running at 19m52s and
    are lower bounds. LPT must never be worse than list order and here
    starts the two longest tables at t=0.
    """
    import heapq

    durations = {
        "demographic": 558, "provider": 20, "harvest": 20, "encounter": 643,
        "diagnosis": 1087, "procedures": 1006, "vital": 1048, "dispensing": 19,
        "lab_result_cm": 1192, "prescribing": 607, "condition": 538, "pro_cm": 71,
        "med_admin": 84, "obs_clin": 563, "obs_gen": 307, "death": 278,
        "death_cause": 274, "lds_address_history": 50, "immunization": 49,
        "hash_token": 37, "enrollment": 77, "external_meds": 41, "lab_history": 5,
        "private_address_history": 38, "private_address_geocode": 24,
        "pat_relationship": 6, "pcornet_trial": 7,
    }  # fmt: skip

    def makespan(order: list[str], slots: int = 8) -> int:
        heap = [0] * slots
        for name in order:
            heapq.heappush(heap, heapq.heappop(heap) + durations[name])
        return max(heap)

    given = list(durations)
    lpt = _names(sort_by_size(_specs(*given), durations))
    assert lpt[:2] == ["lab_result_cm", "diagnosis"]
    assert makespan(lpt) <= makespan(given)
    assert makespan(lpt) == max(durations.values())  # bounded by the largest table
