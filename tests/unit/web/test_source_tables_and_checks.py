"""Tests for copy/move source-table enumeration + the checks submit picker.

Covers three additions:

* ``source_tables_svc`` — resolves a copy/move source address (or an
  inline connection + location) to its concrete table roster, delegating
  the actual enumeration to the trust-safe storage-browse hand-off. Only
  self-describing sources (Access) are ``supported``.
* ``GET /api/v1/sources/tables`` — the HTTP surface the submit-page
  Tables picker calls for ``copy`` / ``move`` jobs.
* The ``/submit`` handler forwards an explicit ``checks`` subset on
  health-shaped kinds so the Advanced per-check picker narrows a run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ovid2.web.models import StorageBrowseEntry, StorageBrowseResponse


# ---------------------------------------------------------------------------
# source_tables_svc
# ---------------------------------------------------------------------------


def _write_access_address(isolated_xdg: Path) -> None:
    """Drop an ``access`` connection + a saved address that references it."""
    conndir = Path(os.environ["OVID2_CONNECTIONS_DIR"])
    (conndir / "drugs_access.json").write_text(
        json.dumps(
            {
                "type": "access",
                "args": {"host_path": "/srv/access", "mount_mode": "ro"},
            }
        ),
        encoding="utf-8",
    )
    cfgdir = Path(os.environ["OVID2_CONFIGS_DIR"])
    (cfgdir / "vrx_src.json").write_text(
        json.dumps(
            {
                "connection": "drugs_access",
                "location": {"path": "drugs.accdb"},
            }
        ),
        encoding="utf-8",
    )


def _write_disc_address(isolated_xdg: Path) -> None:
    """Drop a plain disc connection + address (not an enumerable source)."""
    conndir = Path(os.environ["OVID2_CONNECTIONS_DIR"])
    (conndir / "plain_disc.json").write_text(
        json.dumps(
            {"type": "disc", "args": {"host_path": "/srv/data"}}
        ),
        encoding="utf-8",
    )
    cfgdir = Path(os.environ["OVID2_CONFIGS_DIR"])
    (cfgdir / "disc_src.json").write_text(
        json.dumps(
            {"connection": "plain_disc", "location": {"path": "raw"}}
        ),
        encoding="utf-8",
    )


@pytest.mark.unit
def test_tables_for_address_access_enumerates(isolated_xdg, monkeypatch) -> None:
    _write_access_address(isolated_xdg)
    from ovid2.web.services import source_tables_svc, storage_browse_svc

    def fake_browse(name, prefix=None):
        assert name == "drugs_access"
        assert prefix == "drugs.accdb"
        return StorageBrowseResponse(
            storage=name,
            type="access",
            prefix=prefix or "",
            supported=True,
            entries=[
                StorageBrowseEntry(name="demographic", kind="table", path="x"),
                StorageBrowseEntry(name="drug", kind="table", path="y"),
                # A non-table entry must be filtered out.
                StorageBrowseEntry(name="readme.txt", kind="file", path="z"),
            ],
        )

    monkeypatch.setattr(storage_browse_svc, "browse_storage", fake_browse)
    tables, supported = source_tables_svc.tables_for_address("vrx_src")
    assert supported is True
    assert tables == ["demographic", "drug"]


@pytest.mark.unit
def test_tables_for_address_disc_unsupported(isolated_xdg, monkeypatch) -> None:
    _write_disc_address(isolated_xdg)
    from ovid2.web.services import source_tables_svc, storage_browse_svc

    # Must never even attempt to browse a non-enumerable source.
    def boom(*_a, **_k):  # pragma: no cover - should not be called
        raise AssertionError("browse_storage should not be called for disc")

    monkeypatch.setattr(storage_browse_svc, "browse_storage", boom)
    tables, supported = source_tables_svc.tables_for_address("disc_src")
    assert supported is False
    assert tables == []


@pytest.mark.unit
def test_tables_for_address_unknown_address(isolated_xdg) -> None:
    from ovid2.web.services import source_tables_svc

    tables, supported = source_tables_svc.tables_for_address("does_not_exist")
    assert supported is False
    assert tables == []


@pytest.mark.unit
def test_tables_for_connection_access(isolated_xdg, monkeypatch) -> None:
    _write_access_address(isolated_xdg)
    from ovid2.web.services import source_tables_svc, storage_browse_svc

    monkeypatch.setattr(
        storage_browse_svc,
        "browse_storage",
        lambda name, prefix=None: StorageBrowseResponse(
            storage=name,
            type="access",
            prefix=prefix or "",
            supported=True,
            entries=[StorageBrowseEntry(name="enrollment", kind="table", path="e")],
        ),
    )
    tables, supported = source_tables_svc.tables_for_connection(
        "drugs_access", "drugs.accdb"
    )
    assert supported is True
    assert tables == ["enrollment"]


@pytest.mark.unit
def test_tables_for_address_supported_but_empty(isolated_xdg, monkeypatch) -> None:
    """Enumerable source, but browse returned nothing → supported, empty."""
    _write_access_address(isolated_xdg)
    from ovid2.web.services import source_tables_svc, storage_browse_svc

    monkeypatch.setattr(
        storage_browse_svc,
        "browse_storage",
        lambda name, prefix=None: StorageBrowseResponse(
            storage=name, type="access", prefix=prefix or "",
            supported=True, entries=[],
        ),
    )
    tables, supported = source_tables_svc.tables_for_address("vrx_src")
    assert supported is True
    assert tables == []


# ---------------------------------------------------------------------------
# GET /api/v1/sources/tables
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_http_sources_tables_access(web_client, isolated_xdg, monkeypatch) -> None:
    _write_access_address(isolated_xdg)
    from ovid2.web.services import storage_browse_svc

    monkeypatch.setattr(
        storage_browse_svc,
        "browse_storage",
        lambda name, prefix=None: StorageBrowseResponse(
            storage=name, type="access", prefix=prefix or "",
            supported=True,
            entries=[StorageBrowseEntry(name="lab_result_cm", kind="table", path="l")],
        ),
    )
    resp = web_client.get("/api/v1/sources/tables?address=vrx_src")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["supported"] is True
    assert body["tables"] == ["lab_result_cm"]


@pytest.mark.unit
def test_http_sources_tables_missing_args(web_client, isolated_xdg) -> None:
    resp = web_client.get("/api/v1/sources/tables")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["supported"] is False
    assert body["tables"] == []


# ---------------------------------------------------------------------------
# /submit forwards the checks subset on health-shaped kinds
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_submit_dq_checks_forwards_checks_subset(
    web_client, isolated_xdg
) -> None:
    conndir = Path(os.environ["OVID2_CONNECTIONS_DIR"])
    (conndir / "prod_disc.json").write_text(
        json.dumps({"type": "disc", "args": {"host_path": "/srv/data"}}),
        encoding="utf-8",
    )
    resp = web_client.post(
        "/submit",
        data={
            "kind": "dq_checks",
            "pipeline": "pcornet_edc",
            "batch_id": "20260101",
            "input_mode": "browse",
            "input_connection": "prod_disc",
            "input_location": "char/2026",
            "output_mode": "browse",
            "output_connection": "prod_disc",
            "output_location": "char/results",
            "packages": "20r1",
            "checks": "1.01 1.02",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    assert location.startswith("/runs/"), location
    instance_id = location.rsplit("/", 1)[-1]

    # The persisted job must carry the explicit check subset so the
    # worker forwards it as --checks and the run page pre-seeds it.
    # The submitted job is discoverable by instance id via the jobs API.
    jobs = web_client.get("/api/v1/jobs").json()
    rows = jobs["jobs"] if isinstance(jobs, dict) else jobs
    match = [j for j in rows if j.get("instance_id") == instance_id]
    assert match, f"submitted job {instance_id} not found in {rows}"
    extra = match[0].get("extra_args") or {}
    assert extra.get("checks") == ["1.01", "1.02"]
    assert extra.get("packages") == ["20r1"]


# ---------------------------------------------------------------------------
# /submit seeds the real Access table roster on a copy-all job
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_submit_copy_all_seeds_access_tables(
    web_client, isolated_xdg, monkeypatch
) -> None:
    _write_access_address(isolated_xdg)
    conndir = Path(os.environ["OVID2_CONNECTIONS_DIR"])
    (conndir / "out_disc.json").write_text(
        json.dumps({"type": "disc", "args": {"host_path": "/srv/out"}}),
        encoding="utf-8",
    )
    cfgdir = Path(os.environ["OVID2_CONFIGS_DIR"])
    (cfgdir / "vrx_out.json").write_text(
        json.dumps({"connection": "out_disc", "location": {"path": "vrx"}}),
        encoding="utf-8",
    )
    from ovid2.web.services import source_tables_svc

    # Stub the (subprocess-backed) enumeration so the test stays hermetic.
    monkeypatch.setattr(
        source_tables_svc,
        "tables_for_address",
        lambda addr: (["demographic", "drug"], True)
        if addr == "vrx_src"
        else ([], False),
    )
    resp = web_client.post(
        "/submit",
        data={
            "kind": "copy",
            "pipeline": "copy",
            "batch_id": "20260101",
            "input_mode": "address",
            "input_address": "vrx_src",
            "output_mode": "address",
            "output_address": "vrx_out",
            "tables": "all",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    instance_id = resp.headers["location"].rsplit("/", 1)[-1]
    jobs = web_client.get("/api/v1/jobs").json()
    rows = jobs["jobs"] if isinstance(jobs, dict) else jobs
    match = [j for j in rows if j.get("instance_id") == instance_id]
    assert match, f"submitted job {instance_id} not found"
    # The run page pre-seeds ``job.tables`` — so it must be the *concrete*
    # roster, not the opaque ["all"] sentinel.
    assert match[0]["tables"] == ["demographic", "drug"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run via pytest.")
