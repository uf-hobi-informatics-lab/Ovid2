"""Unit tests for :class:`ovid2.drivers.snowflake.staging.disc.SnowparkDiscStrategy`.

Every test drives the strategy through fake Snowpark session doubles
so the suite runs without live Snowflake credentials. End-to-end
verification against real Snowflake is documented in the CHANGELOG
entry for Phase C.
"""

from __future__ import annotations

import atexit
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ovid2.core.engines.stager import (
    SnowparkEngineStager,
    SnowparkStagingStrategy,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Every test gets a deterministic instance id + clean env."""
    monkeypatch.setenv("OVID2_INSTANCE_ID", "20260806-120000-abc123")
    monkeypatch.delenv("OVID2_SNOWPARK_STAGING_KEEP", raising=False)
    monkeypatch.delenv("OVID2_SNOWPARK_STAGING_INGEST_PREFIX", raising=False)
    monkeypatch.delenv("OVID2_SNOWPARK_STAGING_EGEST_PREFIX", raising=False)


@pytest.fixture(autouse=True)
def _clear_strategy_cleanup_state():
    """Reset the strategy's class-level cleanup registry between tests."""
    from ovid2.drivers.snowflake.staging.disc import SnowparkDiscStrategy

    SnowparkDiscStrategy._cleanups_scheduled.clear()
    SnowparkDiscStrategy._temp_tables.clear()
    yield
    SnowparkDiscStrategy._cleanups_scheduled.clear()
    SnowparkDiscStrategy._temp_tables.clear()


class _FakeReaderChain:
    """Records every .option() / .schema() / .csv() call on a fake reader."""

    def __init__(self) -> None:
        self.options: dict[str, Any] = {}
        self.schema_arg: Any = None
        self.csv_path: str | None = None
        self.returned_df: Any = MagicMock(name="fake_snowpark_df")

    def option(self, key: str, value: Any) -> _FakeReaderChain:
        self.options[key] = value
        return self

    def schema(self, schema_obj: Any) -> _FakeReaderChain:
        self.schema_arg = schema_obj
        return self

    def csv(self, path: str) -> Any:
        self.csv_path = path
        return self.returned_df


class _FakeSession:
    """A ``snowflake.snowpark.Session`` double capturing every side effect."""

    def __init__(self, get_rows: list | None = None) -> None:
        self.puts: list[tuple[str, str, dict[str, Any]]] = []
        self.copy_into_calls: list[dict[str, Any]] = []
        self.sql_calls: list[str] = []
        self._reader = _FakeReaderChain()
        self._get_rows = (
            [_make_get_row("data_0_0_0.csv.gz", "DOWNLOADED")] if get_rows is None else get_rows
        )
        self.tables_returned: dict[str, Any] = {}
        # session.file.put(...)
        self.file = MagicMock()
        self.file.put = self._record_put
        # session.read (a chained reader)
        self.read = self._reader

    def _record_put(
        self,
        source: str,
        target: str,
        *,
        overwrite: bool = False,
        auto_compress: bool = True,
    ) -> list[Any]:
        self.puts.append(
            (source, target, {"overwrite": overwrite, "auto_compress": auto_compress}),
        )
        return [MagicMock()]

    def sql(self, query: str) -> Any:
        self.sql_calls.append(query)
        result = MagicMock()
        if query.startswith("GET "):
            result.collect.return_value = self._get_rows
        else:
            result.collect.return_value = []
        return result

    def table(self, fqtn: str) -> Any:
        df = self.tables_returned.get(fqtn, MagicMock(name=f"fake_df:{fqtn}"))
        self.tables_returned[fqtn] = df
        return df

    def get_current_database(self) -> str:
        return "TESTDB"

    def get_current_schema(self) -> str | None:
        return None  # force _ensure_writable_staging_schema to hit the create-schema branch

    # SnowparkEngineSession-like access shape
    @property
    def native(self) -> Any:
        return self


def _make_get_row(name: str, status: str) -> Any:
    """Emulate a Snowpark Row for GET results."""
    row = MagicMock()
    row.file = name
    row.status = status
    row.__getitem__ = lambda self, k: (
        {"file": name, "status": status}[k] if isinstance(k, str) else (name, 0, status, "")[k]
    )
    return row


class _FakeDiscDriver:
    """Standin for ``DiscDriver`` returning a fixed merged view."""

    type_name = "disc"

    def __init__(self, root: str, single_file: bool = True, fmt: str = "csv") -> None:
        self._merged = {
            "root": root,
            "format": fmt,
            "lowercase_columns": False,
            "single_file": single_file,
        }

    def _merged_location_view(self) -> dict[str, Any]:
        return self._merged


def _write_csv(path: Path, header: str, rows: list[str], sep: str = ",") -> None:
    """Write a small CSV so the strategy has something to PUT."""
    lines = [header] + rows
    path.write_text(sep.join if False else "\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_strategy_registered_under_disc(self) -> None:
        # Importing the driver package triggers strategy registration.
        import ovid2.drivers.snowflake  # noqa: F401

        assert "disc" in SnowparkEngineStager._STRATEGIES
        assert SnowparkEngineStager.supports("disc") is True

    def test_registered_class_is_a_snowpark_strategy(self) -> None:
        import ovid2.drivers.snowflake  # noqa: F401

        cls = SnowparkEngineStager._STRATEGIES["disc"]
        assert issubclass(cls, SnowparkStagingStrategy)
        assert cls.driver_type == "disc"

    def test_snowflake_native_still_registered(self) -> None:
        # Phase A+B's native snowflake strategy must survive the
        # disc addition.
        import ovid2.drivers.snowflake  # noqa: F401

        assert "snowflake" in SnowparkEngineStager._STRATEGIES

    def test_supported_driver_types_reports_both(self) -> None:
        import ovid2.drivers.snowflake  # noqa: F401

        supported = SnowparkEngineStager.supported_driver_types()
        assert "disc" in supported
        assert "snowflake" in supported


# ---------------------------------------------------------------------------
# Import-safety
# ---------------------------------------------------------------------------


class TestImportSafety:
    def test_module_scope_does_not_import_pyspark(self) -> None:
        import sys

        # Fresh import of the strategy module.
        for mod in list(sys.modules):
            if mod == "ovid2.drivers.snowflake.staging.disc":
                del sys.modules[mod]

        # Clear any pyspark cache so we can detect a fresh import.
        pyspark_before = {m for m in sys.modules if m.startswith("pyspark")}
        import ovid2.drivers.snowflake.staging.disc  # noqa: F401

        pyspark_after = {m for m in sys.modules if m.startswith("pyspark")}
        # No new pyspark modules pulled in by the strategy import.
        assert pyspark_after == pyspark_before


# ---------------------------------------------------------------------------
# materialise()
# ---------------------------------------------------------------------------


class TestMaterialise:
    @pytest.fixture
    def strategy(self):
        from ovid2.drivers.snowflake.staging.disc import SnowparkDiscStrategy

        return SnowparkDiscStrategy()

    @pytest.fixture
    def disc_source_tree(self, tmp_path):
        root = tmp_path / "src"
        (root / "TEST" / "20260806").mkdir(parents=True)
        csv_path = root / "TEST" / "20260806" / "sample.csv"
        csv_path.write_text(
            "id,name,value\n1,foo,10\n2,bar,20\n",
            encoding="utf-8",
        )
        return root, csv_path

    def test_puts_file_and_returns_reader_result(
        self,
        strategy,
        disc_source_tree,
    ) -> None:
        root, csv_path = disc_source_tree
        driver = _FakeDiscDriver(str(root))
        session = _FakeSession()

        result = strategy.materialise(
            session,
            driver,
            "test",
            "20260806",
            "sample.csv",
            delimiter=None,
            table_name=None,
        )

        # Result is the .table() lookup for the transient table.
        assert result is not None
        assert len(session.puts) == 1
        src, target, kwargs = session.puts[0]
        assert src == f"file://{csv_path}"
        assert target.startswith("@~/ovid_ingest/20260806-120000-abc123/test/20260806/")
        assert kwargs == {"overwrite": True, "auto_compress": True}

    def test_read_uses_stage_and_copy_into(
        self,
        strategy,
        disc_source_tree,
    ) -> None:
        root, _csv_path = disc_source_tree
        driver = _FakeDiscDriver(str(root))
        session = _FakeSession()

        strategy.materialise(
            session,
            driver,
            "test",
            "20260806",
            "sample.csv",
            delimiter=None,
            table_name=None,
        )

        # A CREATE TRANSIENT TABLE + COPY INTO pair was issued.
        create_stmts = [q for q in session.sql_calls if "CREATE OR REPLACE TRANSIENT TABLE" in q]
        assert len(create_stmts) == 1
        # Columns are quoted but UPPERCASED so an unquoted
        # ``col("id")`` (folded to ``ID``) resolves against the staged
        # column — including after a ``groupBy().agg()`` where Snowpark
        # derives column state strictly. The CSV header ``id,name,value``
        # therefore becomes ``"ID" / "NAME" / "VALUE"``.
        assert '"ID" VARCHAR' in create_stmts[0]
        assert '"NAME" VARCHAR' in create_stmts[0]
        assert '"VALUE" VARCHAR' in create_stmts[0]

        copy_stmts = [q for q in session.sql_calls if q.startswith("COPY INTO ")]
        assert len(copy_stmts) == 1
        copy = copy_stmts[0]
        assert "@~/ovid_ingest/20260806-120000-abc123/test/20260806/sample.csv" in copy
        # Detected delimiter (comma).
        assert "FIELD_DELIMITER = ','" in copy
        assert "SKIP_HEADER = 1" in copy
        assert "TRIM_SPACE = TRUE" in copy

    def test_lowercase_header_columns_are_uppercased_in_ddl(
        self,
        strategy,
        tmp_path,
    ) -> None:
        """A lowercase CSV header stages as uppercase quoted columns.

        Regression guard for the OMOP ``concept_relationship`` failure:
        the export ships lowercase headers (``concept_id_1`` …). If the
        transient table kept that casing (``"concept_id_1"``), an
        unquoted ``col("concept_id_1")`` — folded to ``CONCEPT_ID_1`` —
        would miss after a ``groupBy().agg()`` and Snowflake would raise
        ``invalid identifier 'CONCEPT_ID_1'``. Uppercasing at DDL time
        makes the reference resolve.
        """
        root = tmp_path / "src"
        (root / "test" / "20260806").mkdir(parents=True)
        csv_path = root / "test" / "20260806" / "concept_relationship.csv"
        csv_path.write_text(
            "concept_id_1\tconcept_id_2\trelationship_id\n1\t2\tMaps to\n",
            encoding="utf-8",
        )
        driver = _FakeDiscDriver(str(root))
        session = _FakeSession()

        strategy.materialise(
            session,
            driver,
            "test",
            "20260806",
            "concept_relationship.csv",
            delimiter="\t",
            table_name=None,
        )

        create_stmts = [q for q in session.sql_calls if "CREATE OR REPLACE TRANSIENT TABLE" in q]
        assert len(create_stmts) == 1
        ddl = create_stmts[0]
        assert '"CONCEPT_ID_1" VARCHAR' in ddl
        assert '"CONCEPT_ID_2" VARCHAR' in ddl
        assert '"RELATIONSHIP_ID" VARCHAR' in ddl
        # And the original lowercase form must NOT appear.
        assert '"concept_id_1"' not in ddl

    def test_explicit_delimiter_wins(
        self,
        strategy,
        tmp_path,
    ) -> None:
        root = tmp_path / "src"
        (root / "TEST" / "20260806").mkdir(parents=True)
        csv_path = root / "TEST" / "20260806" / "tabbed.csv"
        csv_path.write_text("id\tname\n1\tfoo\n", encoding="utf-8")

        driver = _FakeDiscDriver(str(root))
        session = _FakeSession()

        strategy.materialise(
            session,
            driver,
            "test",
            "20260806",
            "tabbed.csv",
            delimiter="\t",
            table_name=None,
        )
        copy = next(q for q in session.sql_calls if q.startswith("COPY INTO "))
        # Tab char lands literally in the SQL — Snowflake accepts it.
        assert "FIELD_DELIMITER = '\t'" in copy

    def test_transient_table_registered_for_cleanup(
        self,
        strategy,
        disc_source_tree,
        monkeypatch,
    ) -> None:
        """The transient table is scheduled for DROP on atexit."""
        root, _ = disc_source_tree
        driver = _FakeDiscDriver(str(root))
        session = _FakeSession()
        registered_fns: list[Any] = []
        monkeypatch.setattr(atexit, "register", lambda fn: registered_fns.append(fn))

        strategy.materialise(
            session,
            driver,
            "test",
            "20260806",
            "sample.csv",
            delimiter=None,
            table_name=None,
        )
        assert len(registered_fns) == 1
        # Now invoke cleanup and verify a DROP TABLE was issued.
        registered_fns[0]()
        drops = [q for q in session.sql_calls if q.startswith("DROP TABLE IF EXISTS ")]
        assert len(drops) == 1
        assert "OVID2_STG_" in drops[0]

    def test_non_csv_format_raises(self, strategy, tmp_path) -> None:
        driver = _FakeDiscDriver(str(tmp_path), fmt="parquet")
        session = _FakeSession()
        with pytest.raises(ValueError, match="only supports fmt='csv'"):
            strategy.materialise(
                session,
                driver,
                "test",
                "20260806",
                "sample.parquet",
                delimiter=None,
                table_name=None,
            )

    def test_missing_file_raises(self, strategy, tmp_path) -> None:
        (tmp_path / "TEST" / "20260806").mkdir(parents=True)
        driver = _FakeDiscDriver(str(tmp_path))
        session = _FakeSession()
        with pytest.raises(FileNotFoundError):
            strategy.materialise(
                session,
                driver,
                "test",
                "20260806",
                "nonexistent.csv",
                delimiter=None,
                table_name=None,
            )

    def test_directory_read_not_supported(
        self,
        strategy,
        tmp_path,
    ) -> None:
        # Create a directory that matches a glob.
        (tmp_path / "TEST" / "20260806" / "adir").mkdir(parents=True)
        driver = _FakeDiscDriver(str(tmp_path))
        session = _FakeSession()
        with pytest.raises(ValueError, match="directory reads"):
            strategy.materialise(
                session,
                driver,
                "test",
                "20260806",
                "adir",
                delimiter=None,
                table_name=None,
            )

    def test_empty_file_raises(self, strategy, tmp_path) -> None:
        root = tmp_path / "src"
        (root / "TEST" / "20260806").mkdir(parents=True)
        (root / "TEST" / "20260806" / "empty.csv").write_text("", encoding="utf-8")
        driver = _FakeDiscDriver(str(root))
        session = _FakeSession()
        with pytest.raises(ValueError, match="is empty"):
            strategy.materialise(
                session,
                driver,
                "test",
                "20260806",
                "empty.csv",
                delimiter=None,
                table_name=None,
            )


# ---------------------------------------------------------------------------
# persist()
# ---------------------------------------------------------------------------


class TestPersist:
    @pytest.fixture
    def strategy(self):
        from ovid2.drivers.snowflake.staging.disc import SnowparkDiscStrategy

        return SnowparkDiscStrategy()

    @pytest.fixture
    def disc_target_root(self, tmp_path):
        root = tmp_path / "dst"
        root.mkdir()
        return root

    def _make_df_stub(self, capture: dict[str, Any]) -> MagicMock:
        """Fake DataFrame whose .write.copy_into_location(...) is captured."""
        df = MagicMock(name="snowpark_df")

        def _record_copy(
            location,
            *,
            file_format_type,
            format_type_options,
            header,
            overwrite,
            single,
        ):
            capture["location"] = location
            capture["file_format_type"] = file_format_type
            capture["format_type_options"] = format_type_options
            capture["header"] = header
            capture["overwrite"] = overwrite
            capture["single"] = single

        df.write.copy_into_location = _record_copy
        return df

    def _stage_downloaded_file(
        self,
        target_root: Path,
        pipeline: str,
        batch_id: str,
        name: str,
        content: str = "id,val\n1,x\n",
    ) -> Path:
        """Emulate the file the GET step would produce on disk."""
        out_dir = target_root / pipeline / batch_id
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_calls_copy_into_location_and_renames(
        self,
        strategy,
        disc_target_root,
    ) -> None:
        driver = _FakeDiscDriver(str(disc_target_root), single_file=True)
        capture: dict[str, Any] = {}
        df = self._make_df_stub(capture)

        # Pre-plant the file the GET step will download.
        self._stage_downloaded_file(
            disc_target_root,
            "test",
            "20260806",
            "data_0_0_0.csv",
        )
        session = _FakeSession(
            get_rows=[_make_get_row("data_0_0_0.csv", "DOWNLOADED")],
        )

        strategy.persist(
            session,
            df,
            driver,
            "test",
            "20260806",
            "sample",
            delimiter=None,
        )

        # copy_into_location targets the instance-scoped stage.
        assert capture["location"].startswith(
            "@~/ovid_egest/20260806-120000-abc123/test/20260806/sample/"
        )
        assert capture["file_format_type"] == "csv"
        assert capture["single"] is True
        # Header included; overwrite requested.
        assert capture["header"] is True
        assert capture["overwrite"] is True
        # Delimiter defaults to CSV_DELIMITER (tab).
        assert capture["format_type_options"]["FIELD_DELIMITER"] == "\t"

        # GET SQL was issued once.
        assert any(q.startswith("GET '") for q in session.sql_calls)

        # Result renamed to sample.csv on disk.
        expected = disc_target_root / "test" / "20260806" / "sample.csv"
        assert expected.exists()
        assert expected.read_text(encoding="utf-8") == "id,val\n1,x\n"

    def test_explicit_delimiter_wins(
        self,
        strategy,
        disc_target_root,
    ) -> None:
        driver = _FakeDiscDriver(str(disc_target_root), single_file=True)
        capture: dict[str, Any] = {}
        df = self._make_df_stub(capture)
        self._stage_downloaded_file(
            disc_target_root,
            "test",
            "20260806",
            "data_0_0_0.csv",
        )
        session = _FakeSession(
            get_rows=[_make_get_row("data_0_0_0.csv", "DOWNLOADED")],
        )

        strategy.persist(
            session,
            df,
            driver,
            "test",
            "20260806",
            "sample",
            delimiter="|",
        )
        assert capture["format_type_options"]["FIELD_DELIMITER"] == "|"

    def test_single_file_false_uses_snowflake_multi_shape(
        self,
        strategy,
        disc_target_root,
    ) -> None:
        driver = _FakeDiscDriver(str(disc_target_root), single_file=False)
        capture: dict[str, Any] = {}
        df = self._make_df_stub(capture)
        # Multi-file simulated download.
        self._stage_downloaded_file(
            disc_target_root,
            "test",
            "20260806",
            "data_0_0_0.csv",
        )
        self._stage_downloaded_file(
            disc_target_root,
            "test",
            "20260806",
            "data_0_0_1.csv",
            content="id,val\n2,y\n",
        )
        session = _FakeSession(
            get_rows=[
                _make_get_row("data_0_0_0.csv", "DOWNLOADED"),
                _make_get_row("data_0_0_1.csv", "DOWNLOADED"),
            ],
        )

        strategy.persist(
            session,
            df,
            driver,
            "test",
            "20260806",
            "sample",
            delimiter=None,
        )
        assert capture["single"] is False
        # Multi-part output named sample.part-00000.csv, .part-00001.csv.
        out_dir = disc_target_root / "test" / "20260806"
        assert (out_dir / "sample.part-00000.csv").exists()
        assert (out_dir / "sample.part-00001.csv").exists()

    def test_get_returning_zero_rows_raises(
        self,
        strategy,
        disc_target_root,
    ) -> None:
        driver = _FakeDiscDriver(str(disc_target_root))
        df = self._make_df_stub({})
        session = _FakeSession(get_rows=[])
        with pytest.raises(RuntimeError, match="zero rows"):
            strategy.persist(
                session,
                df,
                driver,
                "test",
                "20260806",
                "sample",
                delimiter=None,
            )

    def test_non_csv_target_raises(
        self,
        strategy,
        disc_target_root,
    ) -> None:
        driver = _FakeDiscDriver(str(disc_target_root), fmt="parquet")
        df = MagicMock()
        session = _FakeSession()
        with pytest.raises(ValueError, match="only supports fmt='csv'"):
            strategy.persist(
                session,
                df,
                driver,
                "test",
                "20260806",
                "sample",
                delimiter=None,
            )

    def test_output_directory_created(
        self,
        strategy,
        disc_target_root,
    ) -> None:
        # Delete pre-created subdirs so we can prove persist creates them.
        driver = _FakeDiscDriver(str(disc_target_root))
        capture: dict[str, Any] = {}
        df = self._make_df_stub(capture)
        # Row references a file we'll pre-stage AFTER persist creates
        # the directory (simulating Snowflake's GET landing files).
        session = _FakeSession(
            get_rows=[_make_get_row("data_0_0_0.csv", "DOWNLOADED")],
        )

        # Pre-stage the file inside a helper that runs during persist
        # by monkey-patching the sql method.
        original_sql = session.sql

        def _sql_that_stages(query: str) -> Any:
            if query.startswith("GET"):
                self._stage_downloaded_file(
                    disc_target_root,
                    "test",
                    "20260806",
                    "data_0_0_0.csv",
                )
            return original_sql(query)

        session.sql = _sql_that_stages

        strategy.persist(
            session,
            df,
            driver,
            "test",
            "20260806",
            "sample",
            delimiter=None,
        )

        expected = disc_target_root / "test" / "20260806" / "sample.csv"
        assert expected.exists()


# ---------------------------------------------------------------------------
# Cleanup / atexit
# ---------------------------------------------------------------------------


class TestCleanup:
    @pytest.fixture
    def strategy(self):
        from ovid2.drivers.snowflake.staging.disc import SnowparkDiscStrategy

        return SnowparkDiscStrategy()

    def test_atexit_registered_on_first_materialise(
        self,
        strategy,
        tmp_path,
        monkeypatch,
    ) -> None:
        root = tmp_path / "src"
        (root / "TEST" / "20260806").mkdir(parents=True)
        (root / "TEST" / "20260806" / "sample.csv").write_text(
            "id\n1\n",
            encoding="utf-8",
        )
        driver = _FakeDiscDriver(str(root))
        session = _FakeSession()

        registered: list[Any] = []
        monkeypatch.setattr(atexit, "register", lambda fn: registered.append(fn))

        strategy.materialise(
            session,
            driver,
            "test",
            "20260806",
            "sample.csv",
            delimiter=None,
            table_name=None,
        )
        assert len(registered) == 1

        # Second call must not register another hook (same instance).
        strategy.materialise(
            session,
            driver,
            "test",
            "20260806",
            "sample.csv",
            delimiter=None,
            table_name=None,
        )
        assert len(registered) == 1

    def test_snowpark_staging_keep_skips_cleanup(
        self,
        strategy,
        tmp_path,
        monkeypatch,
    ) -> None:
        # Toggle the env var and force settings to re-evaluate.
        monkeypatch.setenv("OVID2_SNOWPARK_STAGING_KEEP", "1")
        import importlib

        from ovid2.config import settings as _settings

        importlib.reload(_settings)

        root = tmp_path / "src"
        (root / "TEST" / "20260806").mkdir(parents=True)
        (root / "TEST" / "20260806" / "sample.csv").write_text(
            "id\n1\n",
            encoding="utf-8",
        )
        driver = _FakeDiscDriver(str(root))
        session = _FakeSession()

        registered: list[Any] = []
        monkeypatch.setattr(atexit, "register", lambda fn: registered.append(fn))

        strategy.materialise(
            session,
            driver,
            "test",
            "20260806",
            "sample.csv",
            delimiter=None,
            table_name=None,
        )
        assert registered == []

        # Restore default env for the rest of the suite.
        monkeypatch.delenv("OVID2_SNOWPARK_STAGING_KEEP")
        importlib.reload(_settings)

    def test_cleanup_removes_both_prefixes(
        self,
        strategy,
        tmp_path,
        monkeypatch,
    ) -> None:
        root = tmp_path / "src"
        (root / "TEST" / "20260806").mkdir(parents=True)
        (root / "TEST" / "20260806" / "sample.csv").write_text(
            "id\n1\n",
            encoding="utf-8",
        )
        driver = _FakeDiscDriver(str(root))
        session = _FakeSession()

        registered_fns: list[Any] = []
        monkeypatch.setattr(atexit, "register", lambda fn: registered_fns.append(fn))

        strategy.materialise(
            session,
            driver,
            "test",
            "20260806",
            "sample.csv",
            delimiter=None,
            table_name=None,
        )
        # Invoke the registered cleanup manually and check it issued
        # both REMOVE calls.
        assert len(registered_fns) == 1
        registered_fns[0]()

        remove_calls = [q for q in session.sql_calls if q.startswith("REMOVE")]
        assert len(remove_calls) == 2
        assert any("@~/ovid_ingest/20260806-120000-abc123/" in q for q in remove_calls)
        assert any("@~/ovid_egest/20260806-120000-abc123/" in q for q in remove_calls)

    def test_cleanup_swallows_exceptions(
        self,
        strategy,
        tmp_path,
        monkeypatch,
    ) -> None:
        root = tmp_path / "src"
        (root / "TEST" / "20260806").mkdir(parents=True)
        (root / "TEST" / "20260806" / "sample.csv").write_text(
            "id\n1\n",
            encoding="utf-8",
        )
        driver = _FakeDiscDriver(str(root))

        # Session whose .sql raises on REMOVE.
        class _BadSession(_FakeSession):
            def sql(self, query: str) -> Any:
                if query.startswith("REMOVE"):
                    raise RuntimeError("permission denied")
                return super().sql(query)

        session = _BadSession()

        registered_fns: list[Any] = []
        monkeypatch.setattr(atexit, "register", lambda fn: registered_fns.append(fn))

        strategy.materialise(
            session,
            driver,
            "test",
            "20260806",
            "sample.csv",
            delimiter=None,
            table_name=None,
        )
        # Cleanup must not propagate the exception.
        registered_fns[0]()


# ---------------------------------------------------------------------------
# Header parsing helpers
# ---------------------------------------------------------------------------


class TestReadHeaderColumns:
    def test_basic_split(self, tmp_path) -> None:
        from ovid2.drivers.snowflake.staging.disc import _read_header_columns

        p = tmp_path / "h.csv"
        p.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
        assert _read_header_columns(str(p), ",") == ["a", "b", "c"]

    def test_strips_quotes_and_whitespace(self, tmp_path) -> None:
        from ovid2.drivers.snowflake.staging.disc import _read_header_columns

        p = tmp_path / "h.csv"
        p.write_text('"a", "b" , "c"\n1,2,3\n', encoding="utf-8")
        assert _read_header_columns(str(p), ",") == ["a", "b", "c"]

    def test_synthesises_names_for_empty_fields(self, tmp_path) -> None:
        from ovid2.drivers.snowflake.staging.disc import _read_header_columns

        p = tmp_path / "h.csv"
        p.write_text(",b,\n1,2,3\n", encoding="utf-8")
        assert _read_header_columns(str(p), ",") == ["_c0", "b", "_c2"]

    def test_empty_file_raises(self, tmp_path) -> None:
        from ovid2.drivers.snowflake.staging.disc import _read_header_columns

        p = tmp_path / "h.csv"
        p.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="is empty"):
            _read_header_columns(str(p), ",")


# ---------------------------------------------------------------------------
# Stage path helpers
# ---------------------------------------------------------------------------


class TestStagePaths:
    def test_ingest_uses_instance_and_prefix(self) -> None:
        from ovid2.drivers.snowflake.staging.disc import _ingest_stage_dir

        d = _ingest_stage_dir("20260806-120000-abc123", "test", "20260806")
        assert d == "ovid_ingest/20260806-120000-abc123/test/20260806"

    def test_egest_includes_table(self) -> None:
        from ovid2.drivers.snowflake.staging.disc import _egest_stage_dir

        d = _egest_stage_dir(
            "20260806-120000-abc123",
            "test",
            "20260806",
            "sample",
        )
        assert d == "ovid_egest/20260806-120000-abc123/test/20260806/sample"

    def test_custom_prefix_env_var(self, monkeypatch) -> None:
        monkeypatch.setenv("OVID2_SNOWPARK_STAGING_INGEST_PREFIX", "custom_in")
        import importlib

        from ovid2.config import settings as _settings

        importlib.reload(_settings)

        from ovid2.drivers.snowflake.staging import disc as _disc_strategy_mod

        # Reload the strategy module so it picks up the reloaded settings.
        importlib.reload(_disc_strategy_mod)

        d = _disc_strategy_mod._ingest_stage_dir(
            "20260806-120000-abc123",
            "test",
            "20260806",
        )
        assert d.startswith("custom_in/")

        monkeypatch.delenv("OVID2_SNOWPARK_STAGING_INGEST_PREFIX")
        importlib.reload(_settings)
        importlib.reload(_disc_strategy_mod)


# ---------------------------------------------------------------------------
# GET result parsing
# ---------------------------------------------------------------------------


class TestSortedDownloadedFiles:
    def test_filters_failed_rows(self, tmp_path) -> None:
        from ovid2.drivers.snowflake.staging.disc import _sorted_downloaded_files

        (tmp_path / "a.csv").write_text("x\n", encoding="utf-8")
        rows = [
            _make_get_row("a.csv", "DOWNLOADED"),
            _make_get_row("b.csv", "SKIPPED"),
        ]
        result = _sorted_downloaded_files(rows, str(tmp_path))
        assert result == [str(tmp_path / "a.csv")]

    def test_decompresses_gz(self, tmp_path) -> None:
        import gzip

        from ovid2.drivers.snowflake.staging.disc import _sorted_downloaded_files

        gz_path = tmp_path / "a.csv.gz"
        with gzip.open(gz_path, "wb") as fh:
            fh.write(b"id,v\n1,x\n")
        rows = [_make_get_row("a.csv.gz", "DOWNLOADED")]
        result = _sorted_downloaded_files(rows, str(tmp_path))
        assert result == [str(tmp_path / "a.csv")]
        plain = tmp_path / "a.csv"
        assert plain.exists()
        assert plain.read_text(encoding="utf-8") == "id,v\n1,x\n"
        # Original .gz cleaned up.
        assert not gz_path.exists()

    def test_missing_file_skipped_with_warning(self, tmp_path) -> None:
        from ovid2.drivers.snowflake.staging.disc import _sorted_downloaded_files

        rows = [_make_get_row("missing.csv", "DOWNLOADED")]
        result = _sorted_downloaded_files(rows, str(tmp_path))
        assert result == []


# ---------------------------------------------------------------------------
# Finaliser helpers
# ---------------------------------------------------------------------------


class TestFinaliseSingleFile:
    def test_single_file_renamed(self, tmp_path) -> None:
        from ovid2.drivers.snowflake.staging.disc import _finalise_single_file

        src = tmp_path / "data_0_0_0.csv"
        src.write_text("id,v\n1,x\n", encoding="utf-8")
        _finalise_single_file([str(src)], str(tmp_path), "sample")

        expected = tmp_path / "sample.csv"
        assert expected.exists()
        assert expected.read_text(encoding="utf-8") == "id,v\n1,x\n"
        assert not src.exists()

    def test_multiple_files_concatenated_with_header_dedup(
        self,
        tmp_path,
    ) -> None:
        from ovid2.drivers.snowflake.staging.disc import _finalise_single_file

        src1 = tmp_path / "data_0_0_0.csv"
        src2 = tmp_path / "data_0_0_1.csv"
        src1.write_text("id,v\n1,a\n", encoding="utf-8")
        src2.write_text("id,v\n2,b\n", encoding="utf-8")

        _finalise_single_file([str(src1), str(src2)], str(tmp_path), "sample")

        merged = (tmp_path / "sample.csv").read_text(encoding="utf-8")
        assert merged == "id,v\n1,a\n2,b\n"

    def test_overwrites_existing(self, tmp_path) -> None:
        from ovid2.drivers.snowflake.staging.disc import _finalise_single_file

        # Pre-existing target should be replaced.
        (tmp_path / "sample.csv").write_text("stale", encoding="utf-8")
        src = tmp_path / "data_0_0_0.csv"
        src.write_text("id,v\n1,x\n", encoding="utf-8")
        _finalise_single_file([str(src)], str(tmp_path), "sample")
        assert (tmp_path / "sample.csv").read_text(encoding="utf-8") == "id,v\n1,x\n"


class TestFinaliseMultiPart:
    def test_names_are_zero_padded(self, tmp_path) -> None:
        from ovid2.drivers.snowflake.staging.disc import _finalise_multi_part

        srcs = []
        for i in range(3):
            p = tmp_path / f"data_0_0_{i}.csv"
            p.write_text(f"id,v\n{i},x\n", encoding="utf-8")
            srcs.append(str(p))

        _finalise_multi_part(srcs, str(tmp_path), "sample")

        for i in range(3):
            assert (tmp_path / f"sample.part-{i:05d}.csv").exists()


class TestWritableCurrentDatabase:
    """The ``SNOWFLAKE`` system database must never be a CREATE SCHEMA target."""

    def test_none_returns_none(self) -> None:
        from ovid2.drivers.snowflake.staging.disc import _writable_current_database

        assert _writable_current_database(None) is None

    def test_empty_returns_none(self) -> None:
        from ovid2.drivers.snowflake.staging.disc import _writable_current_database

        assert _writable_current_database("") is None
        assert _writable_current_database('""') is None

    def test_snowflake_system_db_rejected(self) -> None:
        from ovid2.drivers.snowflake.staging.disc import _writable_current_database

        assert _writable_current_database("SNOWFLAKE") is None
        assert _writable_current_database("snowflake") is None
        assert _writable_current_database('"SNOWFLAKE"') is None

    def test_sample_data_db_rejected(self) -> None:
        from ovid2.drivers.snowflake.staging.disc import _writable_current_database

        assert _writable_current_database("SNOWFLAKE_SAMPLE_DATA") is None

    def test_real_database_passes_through_stripped(self) -> None:
        from ovid2.drivers.snowflake.staging.disc import _writable_current_database

        assert _writable_current_database("MYDB") == "MYDB"
        assert _writable_current_database('"MyDb"') == "MyDb"
        assert _writable_current_database("  OMOP_OUT  ") == "OMOP_OUT"


class TestEnsureWritableStagingSchemaRejectsSystemDb:
    """``_ensure_writable_staging_schema`` gives an actionable error on SNOWFLAKE."""

    class _Native:
        def __init__(self, current_db: str | None) -> None:
            self._db = current_db
            self.sql_calls: list[str] = []

        def get_current_database(self) -> str | None:
            return self._db

        def sql(self, query: str) -> Any:
            self.sql_calls.append(query)
            result = MagicMock()
            result.collect.return_value = []
            return result

    def test_snowflake_current_db_raises_without_create_attempt(
        self,
        monkeypatch,
    ) -> None:
        from ovid2.drivers.snowflake.staging.disc import (
            _ensure_writable_staging_schema,
        )

        monkeypatch.delenv("OVID2_SNOWPARK_STAGING_SCHEMA", raising=False)
        native = self._Native("SNOWFLAKE")
        with pytest.raises(RuntimeError) as exc:
            _ensure_writable_staging_schema(native)
        # No doomed CREATE SCHEMA SNOWFLAKE.* attempted.
        assert not any("CREATE SCHEMA" in q for q in native.sql_calls)
        assert "writable current database" in str(exc.value)

    def test_bare_override_with_snowflake_db_raises(self, monkeypatch) -> None:
        from ovid2.drivers.snowflake.staging.disc import (
            _ensure_writable_staging_schema,
        )

        monkeypatch.setenv("OVID2_SNOWPARK_STAGING_SCHEMA", "MY_STAGING")
        native = self._Native("SNOWFLAKE")
        with pytest.raises(RuntimeError) as exc:
            _ensure_writable_staging_schema(native)
        assert "no writable current database" in str(exc.value)

    def test_real_db_creates_staging_schema(self, monkeypatch) -> None:
        from ovid2.drivers.snowflake.staging.disc import (
            _ensure_writable_staging_schema,
        )

        monkeypatch.delenv("OVID2_SNOWPARK_STAGING_SCHEMA", raising=False)
        native = self._Native("OMOP_OUT")
        schema = _ensure_writable_staging_schema(native)
        assert schema == "OMOP_OUT.OVID2_SNOWPARK_STAGING"
        assert any(
            "CREATE SCHEMA IF NOT EXISTS OMOP_OUT.OVID2_SNOWPARK_STAGING" in q
            for q in native.sql_calls
        )

    def test_dotted_override_used_verbatim_even_on_snowflake_session(
        self,
        monkeypatch,
    ) -> None:
        from ovid2.drivers.snowflake.staging.disc import (
            _ensure_writable_staging_schema,
        )

        # A fully-qualified override is honoured regardless of the
        # session's current database (the operator picked it explicitly).
        monkeypatch.setenv("OVID2_SNOWPARK_STAGING_SCHEMA", "OMOP_OUT.STG")
        native = self._Native("SNOWFLAKE")
        assert _ensure_writable_staging_schema(native) == "OMOP_OUT.STG"
