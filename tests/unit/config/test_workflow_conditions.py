"""Workflow branch conditions: validator + evaluator (config/workflow_conditions.py)."""

from __future__ import annotations

from typing import Any

import pytest

from ovid2.config import workflow_conditions as wc

FACTS: dict[str, Any] = {
    "status": "succeeded",
    "error": "",
    "duration_s": 42.5,
    "pipeline": "site_etl",
    "output_address": "store_out",
    "batch_id": "acme/SITE_20261101",
    "tables_completed": ["DEMOGRAPHIC", "ENCOUNTER"],
    "tables_failed": [],
    "tables_completed_count": 2,
    "warnings": 3,
    "tables": {"DEMOGRAPHIC": "completed", "ENCOUNTER": "completed"},
    "health": {
        "available": True,
        "checks_total": 10,
        "checks_passed": 8,
        "checks_failed": 2,
        "pass_rate": 80.0,
        "blockers": 1,
        "failed_by_severity": {"blocker": 1, "warning": 1},
        "checks": {"DC_1_04": "exception", "DC_1_06": "passed"},
        "failed_checks": ["DC_1_04", "DC_2_01"],
    },
    "output": {"tables": ["demographic", "encounter"]},
    "rows": {"demographic": 1200},
    "query": {"female": 600},
}


def leaf(field: str, op: str, value: Any = None, **kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"field": field, "op": op}
    if value is not None:
        out["value"] = value
    out.update(kw)
    return out


@pytest.mark.unit
@pytest.mark.parametrize(
    ("cond", "expected"),
    [
        (leaf("status", "eq", "succeeded"), True),
        (leaf("status", "eq", "SUCCEEDED"), True),
        (leaf("status", "eq", "SUCCEEDED", case_sensitive=True), False),
        (leaf("status", "ne", "failed"), True),
        (leaf("duration_s", "gt", 40), True),
        (leaf("duration_s", "lte", "42.5"), True),
        (leaf("duration_s", "between", [10, 40]), False),
        (leaf("warnings", "in", [1, 2, 3]), True),
        (leaf("warnings", "not_in", [3]), False),
        (leaf("batch_id", "starts_with", "acme/"), True),
        (leaf("batch_id", "ends_with", "20261101"), True),
        (leaf("batch_id", "contains", "site"), True),
        (leaf("batch_id", "not_contains", "site"), False),
        (leaf("batch_id", "matches", r"_\d{8}$"), True),
        (leaf("error", "is_empty"), True),
        (leaf("tables_completed", "contains", "encounter"), True),
        (leaf("tables_completed", "not_contains", "DEATH"), True),
        (leaf("tables_completed", "contains_all", ["DEMOGRAPHIC", "ENCOUNTER"]), True),
        (leaf("tables_completed", "contains_any", ["DEATH", "VITAL"]), False),
        (leaf("tables_completed", "starts_with", "DEMO"), True),
        (leaf("tables_failed", "is_empty"), True),
        (leaf("table.DEMOGRAPHIC.status", "eq", "completed"), True),
        (leaf("table.death.status", "exists"), False),
        (leaf("health.blockers", "eq", 0), False),
        (leaf("health.pass_rate", "gte", 80), True),
        (leaf("health.failed_by_severity.warning", "eq", 1), True),
        (leaf("health.failed_by_severity.info", "eq", 0), True),
        (leaf("health.check.DC_1_04.status", "eq", "exception"), True),
        (leaf("health.failed_checks", "contains", "DC_2_01"), True),
        (leaf("health.available", "is_true"), True),
        (leaf("output.tables", "contains", "DEMOGRAPHIC"), True),
        (leaf("output.table_count", "eq", 2), True),
        (leaf("rows.DEMOGRAPHIC", "gt", 1000), True),
        (leaf("query.female", "gte", 500), True),
        (leaf("query.male", "exists"), False),
        (leaf("rows_written_total", "gt", 0), False),  # unknown → positive test false
        (leaf("rows_written_total", "ne", 0), True),  # unknown → negative test true
    ],
)
def test_leaf_ops(cond: dict[str, Any], expected: bool) -> None:
    assert wc.validate_condition(cond) == []
    assert wc.evaluate(cond, FACTS) is expected


@pytest.mark.unit
def test_boolean_groups() -> None:
    ok = leaf("status", "eq", "succeeded")
    bad = leaf("health.blockers", "eq", 0)
    assert wc.evaluate({"all": [ok, bad]}, FACTS) is False
    assert wc.evaluate({"any": [ok, bad]}, FACTS) is True
    assert wc.evaluate({"not": bad}, FACTS) is True
    nested = {"all": [ok, {"any": [bad, {"not": leaf("tables_failed", "is_not_empty")}]}]}
    assert wc.validate_condition(nested) == []
    assert wc.evaluate(nested, FACTS) is True
    assert wc.evaluate(None, FACTS) is True
    assert "AND" in wc.summarise({"all": [ok, bad]})
    assert wc.explain(bad, FACTS).endswith("(health.blockers = 1)")


@pytest.mark.unit
def test_health_fields_unknown_without_report() -> None:
    facts = {"status": "succeeded", "health": {"available": False}}
    assert wc.evaluate(leaf("health.blockers", "eq", 0), facts) is False
    assert wc.evaluate(leaf("health.blockers", "not_exists"), facts) is True
    assert wc.evaluate(leaf("health.available", "is_false"), facts) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("cond", "needle"),
    [
        (leaf("nope", "eq", 1), "unknown field"),
        (leaf("status", "frobnicate", 1), "unknown operator"),
        (leaf("duration_s", "starts_with", "1"), "does not apply"),
        (leaf("duration_s", "gt", "abc"), "not a number"),
        (leaf("duration_s", "between", [5]), "two values"),
        (leaf("duration_s", "between", [9, 5]), "low value"),
        (leaf("warnings", "in", []), "non-empty list"),
        (leaf("batch_id", "matches", "(["), "invalid regex"),
        (leaf("status", "eq", ["a"]), "single value"),
        ({"all": []}, "non-empty"),
        ({"all": [], "any": []}, "exactly one key"),
        ("status = 1", "must be an object"),
        (leaf("query.zzz", "gt", 1), "no data probe"),
        ({**leaf("status", "eq", "x"), "bogus": 1}, "unknown key"),
    ],
)
def test_validation_errors(cond: Any, needle: str) -> None:
    errors = wc.validate_condition(cond, probes={"female"})
    assert errors and any(needle in e for e in errors), errors


@pytest.mark.unit
def test_depth_and_leaf_limits() -> None:
    deep: dict[str, Any] = leaf("status", "eq", "x")
    for _ in range(wc.MAX_DEPTH + 1):
        deep = {"not": deep}
    assert any("deeper" in e for e in wc.validate_condition(deep))
    wide = {"any": [leaf("warnings", "eq", i) for i in range(wc.MAX_LEAVES + 1)]}
    assert any("at most" in e for e in wc.validate_condition(wide))


@pytest.mark.unit
def test_probe_validation() -> None:
    assert (
        wc.validate_probe(
            {"name": "female", "table": "DEMOGRAPHIC", "agg": "count", "where": "SEX = 'F'"}
        )
        == []
    )
    assert (
        wc.validate_probe(
            {"name": "ids", "table": "DEMOGRAPHIC", "agg": "count_distinct", "column": "PATID"}
        )
        == []
    )
    assert any(
        "needs a column" in e
        for e in wc.validate_probe({"name": "x", "table": "T", "agg": "null_count"})
    )
    assert any(
        "filter rejected" in e
        for e in wc.validate_probe({"name": "x", "table": "T", "where": "(select 1)"})
    )
    assert any(
        "filter rejected" in e
        for e in wc.validate_probe({"name": "x", "table": "T", "where": "count(*) > 1"})
    )
    assert any("name must" in e for e in wc.validate_probe({"name": "Bad Name", "table": "T"}))
    assert any(
        "agg must" in e for e in wc.validate_probe({"name": "x", "table": "T", "agg": "sum"})
    )


@pytest.mark.unit
def test_catalogue_is_consistent() -> None:
    cat = wc.catalogue()
    assert set(cat["ops"]) == set(wc.OPS)
    for ops in cat["ops_by_type"].values():
        assert set(ops) <= set(wc.OPS)
    groups = {f["group"] for f in cat["fields"]}
    assert groups == {"run", "timeline", "health", "output", "analysis", "metrics", "query"}
    assert wc.field_groups(
        {"all": [leaf("health.blockers", "eq", 0), leaf("rows.X", "gt", 1)]}
    ) == {
        "health",
        "analysis",
    }
