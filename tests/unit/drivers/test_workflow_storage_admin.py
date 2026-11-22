"""Workflow-rollback storage admin: object-store mixin + Snowflake SQL shape."""

from __future__ import annotations

from typing import Any

import pytest

from ovid2.drivers._object_store_admin import AdminStore, ObjectStoreAdminMixin

pytestmark = pytest.mark.unit


class MemStore(AdminStore):
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = dict(objects)

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))

    def copy(self, src_key: str, dst_key: str) -> None:
        self.objects[dst_key] = self.objects[src_key]

    def delete(self, keys: list[str]) -> None:
        for k in keys:
            self.objects.pop(k, None)


class FakeObjectDriver(ObjectStoreAdminMixin):
    def __init__(self, store: MemStore, path: str) -> None:
        self.location = {"path": path}
        self.store = store

    def _admin_store(self) -> AdminStore:
        return self.store


def test_object_store_snapshot_restore_drop() -> None:
    store = MemStore(
        {
            "out/person.csv": b"OLD",
            "out/visit.csv/part-0.csv": b"V0",
            "out/visit.csv/part-1.csv": b"V1",
            "out/keep.parquet": b"K",
            "other/person.csv": b"X",
        }
    )
    drv = FakeObjectDriver(store, "out")
    assert FakeObjectDriver.supports_workflow_rollback()
    assert drv.snapshot_tables(["person", "visit", "nope"], "run1") == ["person", "visit"]
    assert store.objects["out/.ovid2_wf_backup/run1/visit.csv/part-1.csv"] == b"V1"
    # The run overwrites person and rewrites visit with fewer parts; adds new.
    store.objects["out/person.csv"] = b"NEW"
    del store.objects["out/visit.csv/part-1.csv"]
    store.objects["out/new.csv"] = b"N"
    assert drv.drop_tables(["new"]) == ["new"]
    assert drv.restore_tables(["person", "visit"], "run1") == ["person", "visit"]
    assert store.objects["out/person.csv"] == b"OLD"
    assert store.objects["out/visit.csv/part-1.csv"] == b"V1"
    drv.drop_backup("run1")
    assert not [k for k in store.objects if ".ovid2_wf_backup" in k]
    assert store.objects["other/person.csv"] == b"X"
    assert store.objects["out/keep.parquet"] == b"K"


def test_snowflake_admin_is_parameterised(monkeypatch: pytest.MonkeyPatch) -> None:
    from ovid2.drivers.snowflake import admin

    calls: list[tuple[str, tuple[Any, ...]]] = []

    class Cur:
        def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
            calls.append((sql, params))
            self._last = (sql, params)

        def fetchall(self) -> list[tuple[str]]:
            _sql, params = self._last
            listing = "INFORMATION_SCHEMA" in str(params) and params[1] == "SITE_X"
            return [("PERSON",), ("VISIT",)] if listing else []

        def close(self) -> None:
            pass

    import contextlib

    @contextlib.contextmanager
    def fake_cursor(args: Any, secrets: Any) -> Any:
        yield Cur()

    monkeypatch.setattr(admin, "_cursor", fake_cursor)
    done = admin.snapshot_tables({}, {}, "DB", "SITE_X", ["person", "missing"], "abcdef1234")
    assert done == ["person"]
    clone = next(c for c in calls if "CLONE" in c[0])
    assert clone[1] == ('"DB"."SITE_X_OVID2BK_ABCDEF12"."PERSON"', '"DB"."SITE_X"."PERSON"')
    for sql, _ in calls:
        assert "SITE_X" not in sql and '"DB"' not in sql
    calls.clear()
    assert admin.drop_tables({}, {}, "DB", "SITE_X", ["visit"]) == ["visit"]
    assert calls[-1] == ("DROP TABLE IF EXISTS IDENTIFIER(%s)", ('"DB"."SITE_X"."VISIT"',))


def test_rollback_capability_flags() -> None:
    from ovid2.drivers.registry import DRIVERS, load_class

    capable = {t for t in DRIVERS if load_class(t).supports_workflow_rollback()}
    assert {"disc", "s3", "azure_blob", "snowflake"} <= capable
