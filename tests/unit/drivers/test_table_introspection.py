"""Unit tests for the Data Space table-introspection layer.

- :mod:`ovid2.drivers._tabular` against real CSV / gz / tsv / parquet /
  JSON-lines files and a ranged object-store source.
- :mod:`ovid2.drivers._sql_introspect` against an in-memory DB-API fake.
- The postgres / mssql / databricks / gcs drivers' capability gating.
- :mod:`ovid2.exports` writers (delimiter, header case, file shape).
"""

from __future__ import annotations

import gzip
import io
import json
from pathlib import Path
from typing import Any

import pytest

from ovid2.drivers import _tabular
from ovid2.drivers.disc.driver import DiscDriver


@pytest.fixture()
def folder(tmp_path: Path) -> Path:
    (tmp_path / "t.csv").write_text("A,B\n1,x\n2,\n3,z\n", encoding="utf-8")
    (tmp_path / "t.tsv").write_text("A\tB\n1\tx\n", encoding="utf-8")
    with gzip.open(tmp_path / "g.csv.gz", "wt", encoding="utf-8") as fh:
        fh.write("C|D\n7|8\n9|10\n")
    (tmp_path / "j.jsonl").write_text(
        json.dumps({"k": 1, "v": "a"}) + "\n" + json.dumps({"k": 2, "w": [1]}) + "\n",
        encoding="utf-8",
    )
    parts = tmp_path / "SPARK.csv"
    parts.mkdir()
    (parts / "part-0.csv").write_text("A,B\n1,2\n", encoding="utf-8")
    (parts / "part-1.csv").write_text("A,B\n3,4\n5,6\n", encoding="utf-8")
    (parts / "_SUCCESS").write_text("")
    (parts / ".part-0.csv.crc").write_text("x")
    return tmp_path


def _disc(root: Path) -> DiscDriver:
    return DiscDriver(args={"host_path": str(root)})


class TestTabularText:
    def test_csv_describe_exact_for_small_file(self, folder: Path) -> None:
        info = _disc(folder).describe_table("t.csv")
        assert info.format == "csv" and info.delimiter == ","
        assert [(c.name, c.type) for c in info.columns] == [("A", "integer"), ("B", "string")]
        assert info.row_count == 3 and info.row_count_exact

    def test_tsv_and_gzip(self, folder: Path) -> None:
        d = _disc(folder)
        assert d.describe_table("t.tsv").delimiter == "\t"
        info = d.describe_table("g.csv.gz")
        assert info.delimiter == "|" and [c.name for c in info.columns] == ["C", "D"]
        assert info.row_count is None and not info.row_count_exact  # gz: no estimate
        assert d.count_rows("g.csv.gz") == 2
        assert d.sample_rows("g.csv.gz").rows == (("7", "8"), ("9", "10"))

    def test_empty_cells_become_none(self, folder: Path) -> None:
        assert _disc(folder).sample_rows("t.csv").rows[1] == ("2", None)

    def test_spark_part_directory(self, folder: Path) -> None:
        d = _disc(folder)
        info = d.describe_table("SPARK.csv")
        assert info.parts == 2 and [c.name for c in info.columns] == ["A", "B"]
        assert d.count_rows("SPARK.csv") == 3
        sample = d.sample_rows("SPARK.csv", 2)
        assert sample.rows == (("1", "2"), ("3", "4")) and sample.truncated

    def test_jsonl(self, folder: Path) -> None:
        d = _disc(folder)
        assert [c.name for c in d.describe_table("j.jsonl").columns] == ["k", "v", "w"]
        assert d.sample_rows("j.jsonl").rows == ((1, "a", None), (2, None, "[1]"))
        assert d.count_rows("j.jsonl") == 2

    def test_row_estimate_for_large_file(self, tmp_path: Path) -> None:
        line = "abcdefghij,0123456789\n"
        n = (_tabular._ESTIMATE_BYTES // len(line)) * 3
        (tmp_path / "big.csv").write_text("A,B\n" + line * n, encoding="utf-8")
        info = _disc(tmp_path).describe_table("big.csv")
        assert not info.row_count_exact
        assert abs((info.row_count or 0) - n) / n < 0.02
        assert _disc(tmp_path).count_rows("big.csv") == n

    def test_sample_cap(self, folder: Path) -> None:
        assert len(_disc(folder).sample_rows("t.csv", 10_000).rows) == 3

    def test_escape_and_missing(self, folder: Path) -> None:
        d = _disc(folder)
        with pytest.raises(ValueError):
            d.describe_table("../etc/passwd")
        with pytest.raises(FileNotFoundError):
            d.describe_table("nope.csv")

    def test_unknown_file_is_stats_only(self, folder: Path) -> None:
        (folder / "x.bin").write_bytes(b"\x00")
        info = _disc(folder).describe_table("x.bin")
        assert not info.previewable and info.size_bytes == 1
        with pytest.raises(ValueError):
            _disc(folder).sample_rows("x.bin")


class TestTabularParquet:
    def test_parquet_footer_stats(self, tmp_path: Path) -> None:
        pa = pytest.importorskip("pyarrow")
        pq = pytest.importorskip("pyarrow.parquet")
        pq.write_table(pa.table({"a": [1, 2, 3], "b": ["x", None, "z"]}), tmp_path / "p.parquet")
        d = _disc(tmp_path)
        info = d.describe_table("p.parquet")
        assert info.row_count == 3 and info.row_count_exact
        assert [(c.name, c.type, c.nullable) for c in info.columns] == [
            ("a", "int64", True),
            ("b", "string", True),
        ]
        assert d.sample_rows("p.parquet").rows == ((1, "x"), (2, None), (3, "z"))
        cols, batches = d.iter_rows("p.parquet", None, batch_size=2)
        assert cols == ["a", "b"] and [len(b) for b in batches] == [2, 1]


class _FakeObjectStore:
    """In-memory flat namespace with ranged reads (counts fetches)."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.fetches: list[tuple[str, int, int]] = []

    def head(self, key: str) -> tuple[int, str | None] | None:
        data = self.objects.get(key)
        return None if data is None else (len(data), "2026-01-01T00:00:00Z")

    def listing(self, prefix: str) -> tuple[list[str], list[tuple[str, int, str | None]]]:
        dirs: set[str] = set()
        files: list[tuple[str, int, str | None]] = []
        for key, data in self.objects.items():
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix) :]
            head, sep, _ = rest.partition("/")
            if sep:
                dirs.add(head)
            else:
                files.append((rest, len(data), "2026-01-01T00:00:00Z"))
        return sorted(dirs), files

    def fetch(self, key: str, start: int, end: int) -> bytes:
        self.fetches.append((key, start, end))
        return self.objects[key][start:end]

    def source(self, base: str = "") -> _tabular.ObjectStoreSource:
        return _tabular.ObjectStoreSource(
            head=self.head, listing=self.listing, fetch=self.fetch, base=base
        )


class TestObjectStoreSource:
    def test_csv_and_part_prefix(self) -> None:
        store = _FakeObjectStore(
            {
                "in/a.csv": b"X,Y\n1,2\n",
                "in/T.csv/part-0.csv": b"X\n1\n",
                "in/T.csv/part-1.csv": b"X\n2\n",
                "in/T.csv/_SUCCESS": b"",
            }
        )
        src = store.source("in")
        assert _tabular.describe_table(src, "a.csv").row_count == 1
        info = _tabular.describe_table(src, "T.csv")
        assert info.parts == 2 and info.modified_at == "2026-01-01T00:00:00Z"
        assert _tabular.count_table_rows(src, "T.csv") == 2

    def test_parquet_reads_footer_by_range(self) -> None:
        pa = pytest.importorskip("pyarrow")
        pq = pytest.importorskip("pyarrow.parquet")
        buf = io.BytesIO()
        pq.write_table(pa.table({"n": list(range(1000))}), buf)
        store = _FakeObjectStore({"p.parquet": buf.getvalue()})
        info = _tabular.describe_table(store.source(), "p.parquet")
        assert info.row_count == 1000
        assert all(e - s <= len(buf.getvalue()) for _k, s, e in store.fetches)

    def test_dotdot_rejected(self) -> None:
        with pytest.raises(ValueError):
            _tabular.describe_table(_FakeObjectStore({}).source(), "../x.csv")

    def test_range_reader_seek(self) -> None:
        data = bytes(range(256)) * 10
        r = _tabular.RangeReader(len(data), lambda s, e: data[s:e], read_ahead=16)
        r.seek(-4, io.SEEK_END)
        assert r.read(4) == data[-4:]
        r.seek(100)
        assert r.read(3) == data[100:103]


# ---------------------------------------------------------------------------
# SQL warehouses
# ---------------------------------------------------------------------------


class _Cursor:
    def __init__(self, db: _FakeDb) -> None:
        self.db = db
        self.description: list[tuple[str]] | None = None
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.db.executed.append((sql, params))
        rows, desc = self.db.answer(sql, params)
        self._rows = list(rows)
        self.description = [(d,) for d in desc]

    def fetchall(self) -> list[tuple[Any, ...]]:
        out, self._rows = self._rows, []
        return out

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows.pop(0) if self._rows else None

    def fetchmany(self, n: int) -> list[tuple[Any, ...]]:
        out, self._rows = self._rows[:n], self._rows[n:]
        return out

    def close(self) -> None:
        pass


class _FakeDb:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.connected: list[str | None] = []

    def connect(self, database: str | None) -> Any:
        self.connected.append(database)
        db = self

        class _Conn:
            def cursor(self) -> _Cursor:
                return _Cursor(db)

            def close(self) -> None:
                pass

        return _Conn()

    def answer(self, sql: str, params: Any) -> tuple[list[tuple[Any, ...]], list[str]]:
        s = sql.lower()
        if "pg_database" in s:
            return [("analytics",), ("postgres",)], ["datname"]
        if "information_schema.schemata" in s:
            return [("public",), ("pg_catalog",), ("cdm",)], ["schema_name"]
        if "from pg_class" in s:
            return [("person", "TABLE", 42, 8192, None), ("v_person", "VIEW", None, 0, None)], [
                "relname",
                "k",
                "r",
                "b",
                "m",
            ]
        if "information_schema.columns" in s:
            return [
                ("person_id", "integer", "NO", None, 32, 0),
                ("name", "character varying", "YES", 50, None, None),
            ], ["c"] * 6
        if s.startswith("select count(*)"):
            return [(43,)], ["count"]
        if s.startswith("select * from"):
            assert '"cdm"."person"' in sql
            return [(1, "a"), (2, "b"), (3, "c")][: int(sql.rsplit(" ", 1)[-1])], [
                "person_id",
                "name",
            ]
        return [], []


class TestSqlIntrospect:
    def test_postgres_browse_and_describe(self) -> None:
        from ovid2.drivers import _sql_introspect as sqli
        from ovid2.drivers.postgres.driver import DIALECT

        db = _FakeDb()
        assert [e.name for e in sqli.list_level(DIALECT, db.connect, None)] == [
            "analytics",
            "postgres",
        ]
        schemas = sqli.list_level(DIALECT, db.connect, "analytics")
        assert [e.path for e in schemas] == ["analytics/cdm", "analytics/public"]
        assert db.connected[-1] == "analytics"  # postgres reconnects per database
        tables = sqli.list_level(DIALECT, db.connect, "analytics/cdm")
        assert tables[0].path == "analytics/cdm/person" and tables[0].row_count == 42
        # Schema bound as a parameter, never interpolated.
        assert db.executed[-1][1] == ("cdm",) and "cdm" not in db.executed[-1][0]

        info = sqli.describe_table(DIALECT, db.connect, "analytics/cdm/person")
        assert [(c.name, c.type, c.nullable) for c in info.columns] == [
            ("person_id", "integer", False),
            ("name", "character varying(50)", True),
        ]
        assert info.row_count == 42 and not info.row_count_exact and info.size_bytes == 8192
        assert info.ddl and 'CREATE TABLE "cdm"."person"' in info.ddl and "NOT NULL" in info.ddl

        sample = sqli.sample_rows(DIALECT, db.connect, "analytics/cdm/person", 2)
        assert sample.rows == ((1, "a"), (2, "b")) and sample.truncated
        assert sqli.count_rows(DIALECT, db.connect, "analytics/cdm/person") == 43

    def test_identifier_quoting(self) -> None:
        from ovid2.drivers.mssql.driver import _dialect

        d = _dialect("format")
        assert d.quote("a]b") == "[a]]b]"
        with pytest.raises(ValueError):
            d.quote("bad\nname")
        assert d.qualified("db", "dbo", "T") == "[db].[dbo].[T]"

    def test_mssql_uses_top_and_three_part_names(self) -> None:
        from ovid2.drivers import _sql_introspect as sqli
        from ovid2.drivers.mssql.driver import _dialect

        db = _FakeDb()
        captured: list[str] = []

        class _C(_Cursor):
            def execute(self, sql: str, params: Any = None) -> None:
                captured.append(sql)
                self._rows = []
                self.description = [("x",)]

        class _Conn:
            def cursor(self) -> _C:
                return _C(db)

            def close(self) -> None:
                pass

        sqli.sample_rows(_dialect("qmark"), lambda _d: _Conn(), "db/dbo/T", 5)
        assert captured[-1] == "SELECT TOP 6 * FROM [db].[dbo].[T]"

    def test_databricks_named_params(self) -> None:
        from ovid2.drivers.databricks.driver import DIALECT

        calls: list[tuple[str, Any]] = []

        class _C:
            def execute(self, sql: str, params: Any = None) -> None:
                calls.append((sql, params))

        DIALECT.execute(_C(), "WHERE a = {p} AND b = {p}", ("x", "y"))
        assert calls == [("WHERE a = :p0 AND b = :p1", {"p0": "x", "p1": "y"})]

    @pytest.mark.parametrize("type_name", ["postgres", "mssql", "databricks", "gcs"])
    def test_missing_sdk_is_unsupported_not_crash(
        self, type_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builtins
        import importlib

        from ovid2.drivers.registry import instantiate

        blocked = ("psycopg", "psycopg2", "pymssql", "pyodbc", "databricks", "google")
        real_import = builtins.__import__
        real_import_module = importlib.import_module

        def _imp(name: str, *a: Any, **k: Any) -> Any:
            if name.split(".")[0] in blocked:
                raise ImportError(name)
            return real_import(name, *a, **k)

        def _imp_mod(name: str, *a: Any, **k: Any) -> Any:
            if name.split(".")[0] in blocked:
                raise ImportError(name)
            return real_import_module(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _imp)
        monkeypatch.setattr(importlib, "import_module", _imp_mod)
        driver = instantiate(type_name, args={"bucket_default": "b"}, secrets={})
        with pytest.raises(NotImplementedError):
            driver.list_dir(None)
        with pytest.raises(NotImplementedError):
            driver.describe_table("a/b/c")


# ---------------------------------------------------------------------------
# Export writers
# ---------------------------------------------------------------------------


class TestExportWriters:
    def _rows(self) -> tuple[list[str], Any]:
        return ["Id", "Name"], iter([[(1, "a"), (2, None)], [(3, "c;d")]])

    def test_csv_options(self, tmp_path: Path) -> None:
        from ovid2.exports import export_rows

        cols, batches = self._rows()
        res = export_rows(cols, batches, tmp_path / "o.csv", delimiter=";", header_case="upper")
        assert res.rows == 3
        assert (tmp_path / "o.csv").read_text().splitlines() == [
            "ID;NAME",
            "1;a",
            "2;",
            '3;"c;d"',
        ]
        assert (tmp_path / "o.csv").stat().st_mode & 0o777 == 0o600
        assert not (tmp_path / "o.csv.part").exists()

    def test_validation(self, tmp_path: Path) -> None:
        from ovid2.exports import ExportError, export_rows

        for kw in ({"fmt": "html"}, {"delimiter": "ab"}, {"header_case": "title"}):
            with pytest.raises(ExportError):
                export_rows(["a"], iter([]), tmp_path / "x", **kw)  # type: ignore[arg-type]

    def test_xlsx(self, tmp_path: Path) -> None:
        openpyxl = pytest.importorskip("openpyxl")
        from ovid2.exports import export_rows

        cols, batches = self._rows()
        export_rows(cols, batches, tmp_path / "o.xlsx", fmt="xlsx", header_case="lower")
        ws = openpyxl.load_workbook(tmp_path / "o.xlsx").active
        assert [c.value for c in ws[1]] == ["id", "name"] and ws.max_row == 4

    def test_pdf_and_docx(self, tmp_path: Path) -> None:
        from ovid2.exports import export_rows

        pytest.importorskip("reportlab")
        cols, batches = self._rows()
        export_rows(cols, batches, tmp_path / "o.pdf", fmt="pdf", title="T")
        assert (tmp_path / "o.pdf").read_bytes().startswith(b"%PDF-")
        docx = pytest.importorskip("docx")
        cols, batches = self._rows()
        export_rows(cols, batches, tmp_path / "o.docx", fmt="docx", title="T", header_case="upper")
        table = docx.Document(str(tmp_path / "o.docx")).tables[0]
        assert table.rows[0].cells[1].text == "NAME" and len(table.rows) == 4

    def test_failed_export_leaves_no_file(self, tmp_path: Path) -> None:
        from ovid2.exports import export_rows

        def _boom() -> Any:
            yield [(1,)]
            raise RuntimeError("store went away")

        with pytest.raises(RuntimeError):
            export_rows(["a"], _boom(), tmp_path / "o.csv")
        assert list(tmp_path.iterdir()) == []
