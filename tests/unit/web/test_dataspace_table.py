"""Tests for the Data Space table detail / sample / count / export endpoints.

The executor broker (``ovid2 tools describe-table …`` subprocess) is
replaced by an in-process call to :func:`ovid2.tools.table_ops` so the
full path — web route → service → broker argv → executor op → driver —
runs without spawning a process.
"""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
from typing import Any

import pytest


class _Fixture:
    def __init__(self, root: Path, calls: list[list[str]]) -> None:
        self.root = root
        self.calls = calls


@pytest.fixture()
def disc_tables(isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Fixture:
    """A disc account with a CSV table, a parquet part-directory and a junk file."""
    root = tmp_path / "disc"
    folder = root / "landing" / "2026_03"
    folder.mkdir(parents=True)
    rows = ["PATID,BIRTH_DATE,SEX"] + [f"P{i},2000-01-{(i % 28) + 1:02d},F" for i in range(250)]
    (folder / "DEMOGRAPHIC.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (folder / "notes.bin").write_bytes(b"\x00\x01")
    pq = pytest.importorskip("pyarrow.parquet")
    pa = pytest.importorskip("pyarrow")
    part_dir = folder / "VITAL.parquet"
    part_dir.mkdir()
    table = pa.table({"patid": ["a", "b", "c"], "ht": [1.5, None, 2.0]})
    pq.write_table(table, part_dir / "part-00000.parquet")
    pq.write_table(table, part_dir / "part-00001.parquet")
    (part_dir / "_SUCCESS").write_text("")

    conns = Path(os.environ["OVID2_CONNECTIONS_DIR"])
    (conns / "inbox.json").write_text(
        json.dumps({"type": "disc", "args": {"host_path": str(root)}}), encoding="utf-8"
    )
    monkeypatch.setenv("OVID2_WEB_EXPORT_DIR", str(tmp_path / "exports"))

    from ovid2.tools import table_ops
    from ovid2.web.services import dataspace_table_svc

    calls: list[list[str]] = []

    def _fake_run_tool_json(tool_args: list[str], *, timeout: float = 30.0) -> dict[str, Any]:
        del timeout
        calls.append(list(tool_args))
        op = tool_args[0]
        connection = tool_args[1]
        opts = dict(zip(tool_args[2::2], tool_args[3::2], strict=False))
        path = opts["--path"]
        if op == "describe-table":
            return table_ops.run_table_op("describe", connection, path)
        if op == "sample-table":
            return table_ops.run_table_op("sample", connection, path, limit=int(opts["--limit"]))
        if op == "count-rows":
            return table_ops.run_table_op("count", connection, path)
        if op == "export-table":
            return table_ops.run_table_op(
                "export",
                connection,
                path,
                limit=int(opts["--limit"]) if "--limit" in opts else None,
                fmt=opts["--format"],
                delimiter=opts["--delimiter"],
                header_case=opts["--header-case"],
                out=opts["--out"],
            )
        raise AssertionError(op)

    monkeypatch.setattr(dataspace_table_svc, "run_tool_json", _fake_run_tool_json)
    dataspace_table_svc._CACHE.clear()
    return _Fixture(root, calls)


TABLE = "/inbox/landing/2026_03/DEMOGRAPHIC.csv"


class TestDescribe:
    def test_csv_stats_and_fields(self, web_client, disc_tables) -> None:
        r = web_client.get("/api/v1/dataspace/table", params={"path": TABLE})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "DEMOGRAPHIC.csv"
        assert body["type"] == "disc"
        assert body["format"] == "csv"
        assert [c["name"] for c in body["columns"]] == ["PATID", "BIRTH_DATE", "SEX"]
        assert body["columns"][1]["type"] == "date"
        assert body["row_count"] == 250 and body["row_count_exact"] is True
        assert body["size_bytes"] > 0 and body["modified_at"].endswith("Z")
        assert body["ddl"] is None
        assert body["export_limits"]["csv"] >= body["export_limits"]["pdf"]
        # The broker was asked with the listing path, not the Data Space path.
        assert disc_tables.calls[-1] == [
            "describe-table",
            "inbox",
            "--path",
            "landing/2026_03/DEMOGRAPHIC.csv",
        ]

    def test_parquet_part_directory(self, web_client, disc_tables) -> None:
        r = web_client.get(
            "/api/v1/dataspace/table", params={"path": "/inbox/landing/2026_03/VITAL.parquet"}
        )
        body = r.json()
        assert body["format"] == "parquet"
        assert body["parts"] == 2
        assert body["row_count"] == 6 and body["row_count_exact"] is True
        assert [(c["name"], c["type"]) for c in body["columns"]] == [
            ("patid", "string"),
            ("ht", "double"),
        ]

    def test_unknown_file_is_stats_only(self, web_client, disc_tables) -> None:
        body = web_client.get(
            "/api/v1/dataspace/table", params={"path": "/inbox/landing/2026_03/notes.bin"}
        ).json()
        assert body["previewable"] is False
        assert body["size_bytes"] == 2

    def test_missing_table_400(self, web_client, disc_tables) -> None:
        r = web_client.get("/api/v1/dataspace/table", params={"path": "/inbox/landing/nope.csv"})
        assert r.status_code == 400

    def test_account_root_is_not_a_table(self, web_client, disc_tables) -> None:
        assert (
            web_client.get("/api/v1/dataspace/table", params={"path": "/inbox"}).status_code == 400
        )

    def test_escape_rejected(self, web_client, disc_tables) -> None:
        r = web_client.get("/api/v1/dataspace/table", params={"path": "/inbox/../etc/passwd"})
        assert r.status_code == 400

    def test_detail_is_cached(self, web_client, disc_tables) -> None:
        web_client.get("/api/v1/dataspace/table", params={"path": TABLE})
        web_client.get("/api/v1/dataspace/table", params={"path": TABLE})
        assert sum(1 for c in disc_tables.calls if c[0] == "describe-table") == 1
        web_client.get("/api/v1/dataspace/table", params={"path": TABLE, "refresh": "true"})
        assert sum(1 for c in disc_tables.calls if c[0] == "describe-table") == 2


class TestSampleAndCount:
    def test_sample(self, web_client, disc_tables) -> None:
        body = web_client.get(
            "/api/v1/dataspace/table/sample", params={"path": TABLE, "limit": 5}
        ).json()
        assert body["columns"] == ["PATID", "BIRTH_DATE", "SEX"]
        assert body["rows"][0] == ["P0", "2000-01-01", "F"]
        assert len(body["rows"]) == 5 and body["truncated"] is True

    def test_sample_parquet_nulls(self, web_client, disc_tables) -> None:
        body = web_client.get(
            "/api/v1/dataspace/table/sample",
            params={"path": "/inbox/landing/2026_03/VITAL.parquet", "limit": 10},
        ).json()
        assert body["rows"][1] == ["b", None]
        assert len(body["rows"]) == 6 and body["truncated"] is False

    def test_sample_limit_bounds(self, web_client, disc_tables) -> None:
        r = web_client.get("/api/v1/dataspace/table/sample", params={"path": TABLE, "limit": 5000})
        assert r.status_code == 422

    def test_count(self, web_client, disc_tables) -> None:
        body = web_client.get("/api/v1/dataspace/table/count", params={"path": TABLE}).json()
        assert body == {
            "path": TABLE,
            "row_count": 250,
            "exact": True,
            "supported": True,
            "message": None,
        }


def _export(web_client, **params: Any):  # type: ignore[no-untyped-def]
    return web_client.get("/api/v1/dataspace/table/export", params={"path": TABLE, **params})


class TestExport:
    def test_csv_delimiter_and_lower_header(self, web_client, disc_tables) -> None:
        r = _export(web_client, format="csv", delimiter=";", header_case="lower", limit=3)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/csv")
        assert 'filename="DEMOGRAPHIC.csv"' in r.headers["content-disposition"]
        assert r.headers["x-ovid2-export-rows"] == "3"
        lines = r.text.strip().splitlines()
        assert lines[0] == "patid;birth_date;sex"
        assert lines[1] == "P0;2000-01-01;F"
        assert len(lines) == 4

    def test_tab_delimiter_upper_header(self, web_client, disc_tables) -> None:
        r = _export(web_client, format="csv", delimiter="tab", header_case="upper", limit=2)
        rows = list(csv.reader(io.StringIO(r.text), delimiter="\t"))
        assert rows[0] == ["PATID", "BIRTH_DATE", "SEX"]

    def test_xlsx(self, web_client, disc_tables) -> None:
        openpyxl = pytest.importorskip("openpyxl")
        r = _export(web_client, format="xlsx", header_case="lower")
        assert r.status_code == 200
        wb = openpyxl.load_workbook(io.BytesIO(r.content))
        ws = wb.active
        assert ws.max_row == 251
        assert [c.value for c in ws[1]] == ["patid", "birth_date", "sex"]

    def test_pdf(self, web_client, disc_tables) -> None:
        pytest.importorskip("reportlab")
        r = _export(web_client, format="pdf", limit=20)
        assert r.status_code == 200
        assert r.content.startswith(b"%PDF-")
        assert r.headers["content-type"] == "application/pdf"

    def test_docx(self, web_client, disc_tables) -> None:
        docx = pytest.importorskip("docx")
        r = _export(web_client, format="docx", limit=10, header_case="upper")
        assert r.status_code == 200
        document = docx.Document(io.BytesIO(r.content))
        table = document.tables[0]
        assert len(table.rows) == 11
        assert table.rows[0].cells[0].text == "PATID"

    def test_limit_clamped_to_format_cap(
        self, web_client, disc_tables, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OVID2_EXPORT_MAX_ROWS", "7")
        r = _export(web_client, format="csv", limit=100)
        assert r.headers["x-ovid2-export-rows"] == "7"

    def test_bad_format_422(self, web_client, disc_tables) -> None:
        assert _export(web_client, format="html").status_code == 422

    def test_bad_delimiter_400(self, web_client, disc_tables) -> None:
        assert _export(web_client, format="csv", delimiter="ab").status_code == 400

    def test_export_is_audited_and_private(self, web_client, disc_tables, tmp_path: Path) -> None:
        _export(web_client, format="csv", limit=2)
        files = list((tmp_path / "exports").rglob("*.csv"))
        assert files and all((p.stat().st_mode & 0o077) == 0 for p in files)
        from ovid2.web import db as web_db

        with web_db.connect() as conn:
            row = conn.execute(
                "SELECT action, target FROM web_audit WHERE action = 'dataspace.export'"
            ).fetchone()
        assert row is not None and row["target"] == TABLE


class TestExportJob:
    def test_queue_and_build_argv(self, web_client, disc_tables) -> None:
        r = web_client.post(
            "/api/v1/dataspace/table/export-jobs",
            json={"path": TABLE, "format": "csv", "delimiter": "|", "header_case": "upper"},
        )
        assert r.status_code == 201, r.text
        job_id = r.json()["job_id"]
        from ovid2.web.services import jobs_svc
        from ovid2.web.worker import Worker

        job = jobs_svc.get_job(job_id)
        assert job is not None and job.extra_args["kind"] == "export"
        argv = Worker(ovid2_bin=["ovid2"])._build_argv(job)
        assert argv[:4] == ["ovid2", "tools", "export-table", "inbox"]
        assert argv[argv.index("--path") + 1] == "landing/2026_03/DEMOGRAPHIC.csv"
        assert argv[argv.index("--delimiter") + 1] == "|"
        assert "--limit" not in argv  # whole table
        # Not downloadable until done.
        assert web_client.get(f"/api/v1/jobs/{job_id}/artifact").status_code == 404

    def test_artifact_download_after_done(self, web_client, disc_tables) -> None:
        r = web_client.post(
            "/api/v1/dataspace/table/export-jobs", json={"path": TABLE, "format": "csv"}
        )
        job_id = r.json()["job_id"]
        from ovid2.tools import table_ops
        from ovid2.web.services import jobs_svc

        job = jobs_svc.get_job(job_id)
        assert job is not None
        out = table_ops.run_table_op(
            "export",
            "inbox",
            "landing/2026_03/DEMOGRAPHIC.csv",
            fmt="csv",
            out=job.extra_args["artifact"],
        )
        assert out["ok"] and out["export"]["rows"] == 250
        jobs_svc.mark_terminal(job_id, status=jobs_svc.STATUS_DONE)
        dl = web_client.get(f"/api/v1/jobs/{job_id}/artifact")
        assert dl.status_code == 200
        assert 'filename="DEMOGRAPHIC.csv"' in dl.headers["content-disposition"]
        assert len(dl.text.strip().splitlines()) == 251

    def test_pdf_job_rejected(self, web_client, disc_tables) -> None:
        r = web_client.post(
            "/api/v1/dataspace/table/export-jobs", json={"path": TABLE, "format": "pdf"}
        )
        assert r.status_code == 400

    def test_export_kind_rejected_on_runs(self, web_client, disc_tables) -> None:
        r = web_client.post(
            "/api/v1/runs",
            json={"kind": "export", "pipeline": "x", "input_address": "a", "output_address": "b"},
        )
        assert r.status_code == 400
        assert "export" in r.json()["detail"]
