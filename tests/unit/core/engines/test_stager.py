"""Unit tests for :mod:`ovid2.core.engines.stager`.

Covers the engine-scoped data-plane bridge introduced in Phase A+B of
the engine/storage-orthogonality plan.

Scope:

* :class:`EngineStager.supports` classmethod on both concrete stagers
  returns the right membership without instantiating a real engine.
* :class:`SparkEngineStager` passes through to
  :meth:`Driver.read` / :meth:`Driver.write` verbatim (zero
  behaviour change).
* :class:`SnowparkEngineStager` dispatches on ``driver.type_name``
  and raises :class:`EngineStagerUnsupported` for unregistered types.
* :func:`register_snowpark_strategy` plugs new strategies into the
  dispatch map (this is what Phase C/D will use).
* :func:`stager_class_for_engine_family` /
  :func:`stager_class_for_engine_type` return the right class.

The tests deliberately avoid importing pyspark or snowpark so this
module can run on a bare Python install (the stager itself is
import-safe; the strategies are lazy).
"""

from __future__ import annotations

from typing import Any

import pytest

from ovid2.core.engines.stager import (
    EngineStager,
    EngineStagerUnsupported,
    SnowparkEngineStager,
    SnowparkStagingStrategy,
    SparkEngineStager,
    register_snowpark_strategy,
    stager_class_for_engine_family,
    stager_class_for_engine_type,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeSession:
    """Bare marker object standing in for :class:`EngineSession`.

    Records ``.table()`` and ``.sql()`` invocations so the Phase-C
    ``_NativeSnowparkStrategy`` tests can assert on them. Older
    tests that only need an opaque marker keep working — they never
    touch ``.table`` or ``.sql``.
    """

    def __init__(self) -> None:
        self.table_calls: list[str] = []
        self.sql_calls: list[str] = []

    def table(self, fqtn: str) -> str:
        self.table_calls.append(fqtn)
        return f"table:{fqtn}"

    def sql(self, query: str) -> Any:
        self.sql_calls.append(query)

        class _R:
            def collect(self_inner):  # noqa: N805
                return []

        return _R()

    @property
    def native(self) -> _FakeSession:
        return self


class _ContextTrackingSession:
    """Session double that models Snowflake's current DB/schema state.

    ``USE DATABASE`` / ``USE SCHEMA`` SQL mutate the tracked context;
    ``get_current_database`` / ``get_current_schema`` read it back. Used
    to prove :func:`_NativeSnowparkStrategy.persist` snapshots and
    restores the session context around ``save_as_table``.
    """

    def __init__(self, *, db: str, schema: str) -> None:
        self.current_database = db
        self.current_schema = schema
        self.sql_calls: list[str] = []

    def get_current_database(self) -> str:
        return self.current_database

    def get_current_schema(self) -> str:
        return self.current_schema

    def sql(self, query: str) -> Any:
        self.sql_calls.append(query)
        upper = query.strip().upper()
        if upper.startswith("USE DATABASE "):
            self.current_database = query.strip()[len("USE DATABASE ") :].strip()
        elif upper.startswith("USE SCHEMA "):
            self.current_schema = query.strip()[len("USE SCHEMA ") :].strip()

        class _R:
            def collect(self_inner):  # noqa: N805
                return []

        return _R()

    @property
    def native(self) -> _ContextTrackingSession:
        return self


class _FakeSnowparkDataFrame:
    """Captures ``.write.save_as_table`` calls."""

    def __init__(self, columns: list[str] | None = None) -> None:
        self.write = _FakeDataFrameWriter()
        self.columns = columns if columns is not None else []
        self.temp_views: list[str] = []

    def create_or_replace_temp_view(self, name: str) -> None:
        self.temp_views.append(name)


class _FakeDataFrameWriter:
    def __init__(self) -> None:
        self.save_as_table_calls: list[tuple[Any, dict[str, Any]]] = []
        self.copy_into_location_calls: list[tuple[Any, dict[str, Any]]] = []

    def save_as_table(self, name, **kwargs) -> None:
        self.save_as_table_calls.append((name, kwargs))

    def copy_into_location(self, location, **kwargs) -> None:
        self.copy_into_location_calls.append((location, kwargs))


class _FakeDriverWithLocation:
    """Driver double that carries a ``.location`` dict.

    ``_NativeSnowparkStrategy`` reads ``database`` / ``schema`` off
    the driver's location; this fake exposes them without requiring
    a full :class:`SnowflakeDriver` instantiation.
    """

    def __init__(self, type_name: str, location: dict[str, Any]) -> None:
        self.type_name = type_name
        self.location = location
        self.read_calls: list[tuple[Any, ...]] = []
        self.write_calls: list[tuple[Any, ...]] = []


class _FakeEngine:
    """Enough of :class:`BaseEngine` for the stager to hold a reference."""

    def __init__(self) -> None:
        self._session = _FakeSession()
        self.get_session_calls = 0

    def get_session(self) -> _FakeSession:
        self.get_session_calls += 1
        return self._session


class _FakeDriver:
    """Records ``read`` / ``write`` calls so a pass-through test can assert on them."""

    def __init__(self, type_name: str = "disc") -> None:
        self.type_name = type_name
        self.read_calls: list[tuple[Any, ...]] = []
        self.write_calls: list[tuple[Any, ...]] = []

    def read(
        self,
        session: Any,
        pipeline: str,
        batch_id: str,
        file_name: str,
        delimiter: str | None = None,
        *,
        table_name: str | None = None,
    ) -> str:
        self.read_calls.append(
            (session, pipeline, batch_id, file_name, delimiter, table_name),
        )
        return f"df:{file_name}"

    def write(
        self,
        df: Any,
        session: Any,
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str | None = None,
    ) -> None:
        self.write_calls.append(
            (df, session, pipeline, batch_id, table_name, delimiter),
        )


# ---------------------------------------------------------------------------
# SparkEngineStager
# ---------------------------------------------------------------------------


class TestSparkStager:
    def test_supports_every_registered_driver(self) -> None:
        from ovid2.drivers.registry import DRIVERS

        for name in DRIVERS:
            assert SparkEngineStager.supports(name), name

    def test_supports_rejects_unknown_type(self) -> None:
        assert SparkEngineStager.supports("not_a_driver_type") is False

    def test_supported_driver_types_equals_registry(self) -> None:
        from ovid2.drivers.registry import DRIVERS

        assert SparkEngineStager.supported_driver_types() == frozenset(DRIVERS)

    def test_materialise_is_pass_through(self) -> None:
        engine = _FakeEngine()
        stager = SparkEngineStager(engine)
        driver = _FakeDriver("disc")
        df = stager.materialise(
            driver,
            "demo",
            "20260101",
            "*ENC*",
            "|",
            table_name="ENCOUNTER",
        )
        assert df == "df:*ENC*"
        assert driver.read_calls == [
            (engine._session, "demo", "20260101", "*ENC*", "|", "ENCOUNTER"),
        ]
        # get_session called once for the read leg.
        assert engine.get_session_calls == 1

    def test_persist_is_pass_through(self) -> None:
        engine = _FakeEngine()
        stager = SparkEngineStager(engine)
        driver = _FakeDriver("snowflake")
        stager.persist(
            "some_df",
            driver,
            "demo",
            "20260101",
            "ENCOUNTER",
            "|",
        )
        assert driver.write_calls == [
            ("some_df", engine._session, "demo", "20260101", "ENCOUNTER", "|"),
        ]

    def test_family_marker(self) -> None:
        assert SparkEngineStager.family == "spark"


# ---------------------------------------------------------------------------
# SnowparkEngineStager
# ---------------------------------------------------------------------------


class TestSnowparkStager:
    def test_supports_snowflake(self) -> None:
        assert SnowparkEngineStager.supports("snowflake") is True

    def test_supports_disc_since_phase_c(self) -> None:
        """Phase C landed :class:`SnowparkDiscStrategy`.

        Import the snowflake driver package to guarantee the
        strategy's self-registration side-effect has fired.
        """
        import ovid2.drivers.snowflake  # noqa: F401

        assert SnowparkEngineStager.supports("disc") is True

    def test_supports_s3_since_phase_d(self) -> None:
        """Phase D landed :class:`SnowparkS3Strategy`."""
        import ovid2.drivers.snowflake  # noqa: F401

        assert SnowparkEngineStager.supports("s3") is True

    def test_supports_azure_blob_since_phase_d(self) -> None:
        """Phase D landed :class:`SnowparkAzureBlobStrategy`."""
        import ovid2.drivers.snowflake  # noqa: F401

        assert SnowparkEngineStager.supports("azure_blob") is True

    def test_supports_databricks_since_phase_e(self) -> None:
        """Phase E landed :class:`SnowparkDatabricksStrategy`."""
        import ovid2.drivers.snowflake  # noqa: F401

        assert SnowparkEngineStager.supports("databricks") is True

    def test_supports_gcs_since_phase_f(self) -> None:
        """Phase F landed :class:`SnowparkGcsStrategy`."""
        import ovid2.drivers.snowflake  # noqa: F401

        assert SnowparkEngineStager.supports("gcs") is True

    def test_does_not_support_postgres_today(self) -> None:
        """Post-Phase-F residual: postgres/mssql strategies not yet landed."""
        assert SnowparkEngineStager.supports("postgres") is False
        assert SnowparkEngineStager.supports("mssql") is False

    def test_supported_driver_types_covers_phase_a_b_c_d_e_f(self) -> None:
        """Post-Phase-F the set is at least the six wired entries.

        Keep the assertion open-ended so future additions don't need
        a matching test edit — but the currently wired entries MUST
        be present.
        """
        import ovid2.drivers.snowflake  # noqa: F401

        supported = SnowparkEngineStager.supported_driver_types()
        assert "snowflake" in supported
        assert "disc" in supported
        assert "s3" in supported
        assert "azure_blob" in supported
        assert "databricks" in supported
        assert "gcs" in supported

    def test_materialise_dispatches_to_native_strategy_for_snowflake(self) -> None:
        """Native Snowpark→Snowflake read uses ``session.table(fqtn)``.

        The pre-Phase-C ``_NativeSnowparkStrategy`` was a pass-through
        to :meth:`driver.read` (Spark-only ``.format('snowflake')``).
        Phase C rewires it to call ``session.table(fqtn)`` directly
        so a Snowpark session can materialise a Snowflake table
        without ever loading pyspark. This test locks that in.
        """
        engine = _FakeEngine()
        stager = SnowparkEngineStager(engine)
        driver = _FakeDriverWithLocation(
            "snowflake",
            {"database": "MYDB", "schema": "MYSCHEMA"},
        )
        df = stager.materialise(
            driver,
            "demo",
            "20260101",
            "DEMOGRAPHIC",
            None,
            table_name="DEMOGRAPHIC",
        )
        # session.table('MYDB.MYSCHEMA.DEMOGRAPHIC') was called.
        assert engine._session.table_calls == ["MYDB.MYSCHEMA.DEMOGRAPHIC"]
        # driver.read must NOT be called under the native strategy.
        assert driver.read_calls == []
        assert df == "table:MYDB.MYSCHEMA.DEMOGRAPHIC"

    def test_materialise_native_strategy_applies_string_cast_end_to_end(
        self,
    ) -> None:
        """``stager.materialise`` → native strategy → all-VARCHAR read.

        Wires the full dispatch path (not just the ``_all_string_table``
        helper): a Snowflake driver goes through
        :class:`_NativeSnowparkStrategy` which must project every source
        column through a string cast, so the produced DataFrame matches
        the disc reader's all-string contract and downstream unions never
        coerce ``''`` into a native ``NUMBER`` column.
        """
        cast_calls: list[str] = []

        class _FakeCol:
            def __init__(self, name: str) -> None:
                self._name = name

            def cast(self, t: str) -> _FakeCol:
                cast_calls.append(f"{self._name}:{t}")
                return self

            def alias(self, name: str) -> _FakeCol:
                return self

        class _NativeTable:
            columns = ["RX_QUANTITY", "PATID"]

            def __getitem__(self, name: str) -> _FakeCol:
                return _FakeCol(name)

            def select(self, *args: Any) -> _NativeTable:
                return self

        class _CastingSession:
            def __init__(self) -> None:
                self.table_calls: list[str] = []

            def table(self, fqtn: str) -> _NativeTable:
                self.table_calls.append(fqtn)
                return _NativeTable()

            @property
            def native(self) -> _CastingSession:
                return self

        engine = _FakeEngine()
        engine._session = _CastingSession()
        stager = SnowparkEngineStager(engine)
        driver = _FakeDriverWithLocation(
            "snowflake",
            {"database": "MYDB", "schema": "MYSCHEMA"},
        )
        result = stager.materialise(
            driver,
            "demo",
            "20260101",
            "PRESCRIBING",
            None,
            table_name="PRESCRIBING",
        )
        assert engine._session.table_calls == ["MYDB.MYSCHEMA.PRESCRIBING"]
        assert isinstance(result, _NativeTable)
        assert cast_calls == ["RX_QUANTITY:string", "PATID:string"]

    def test_materialise_casts_native_columns_to_string(self) -> None:
        """Native Snowflake reads are coerced to all-VARCHAR.

        Regression guard for the live ``drug_exposure`` Snowpark failure
        ``100038 (22018): Numeric value '' is not recognized``. A native
        Snowflake source table carries its declared column types
        (``NUMBER`` …); when a fan-in formatter unions such a leg with a
        leg supplying ``''`` for the same column, Snowflake coerces the
        empty string into ``NUMBER`` and aborts. Casting every source
        column to ``VARCHAR`` at materialise time makes the native read
        behave like the disc read (all-string), so unions never coerce
        and the typed CTAS ``TRY_CAST`` handles numeric conversion
        leniently.
        """
        from ovid2.core.engines import stager as stager_mod

        cast_calls: list[str] = []

        class _FakeCol:
            def __init__(self, name: str) -> None:
                self._name = name

            def cast(self, t: str) -> _FakeCol:
                cast_calls.append(f"{self._name}:{t}")
                return self

            def alias(self, name: str) -> _FakeCol:
                self._name = name
                return self

        class _FakeNativeTable:
            def __init__(self) -> None:
                self.columns = ["MEDADMINID", "MEDADMIN_DOSE_ADMIN", "PATID"]
                self.select_args: list[Any] | None = None

            def __getitem__(self, name: str) -> _FakeCol:
                return _FakeCol(name)

            def select(self, *args: Any) -> _FakeNativeTable:
                self.select_args = list(args)
                return self

        table = _FakeNativeTable()

        class _CastingNative:
            def table(self, fqtn: str) -> _FakeNativeTable:
                return table

        result = stager_mod._all_string_table(
            _CastingNative(),
            "MYDB.MYSCHEMA.MED_ADMIN",
        )

        # Every column cast to string.
        assert cast_calls == [
            "MEDADMINID:string",
            "MEDADMIN_DOSE_ADMIN:string",
            "PATID:string",
        ]
        # The projection was applied (one aliased col per source column).
        assert result.select_args is not None
        assert len(result.select_args) == 3

    def test_all_string_table_no_op_when_no_columns(self) -> None:
        """A table exposing no columns is returned unchanged (no import)."""
        from ovid2.core.engines import stager as stager_mod

        class _NoColTable:
            columns: list[str] = []

        class _Native:
            def table(self, fqtn: str) -> _NoColTable:
                return _NoColTable()

        native = _Native()
        result = stager_mod._all_string_table(native, "D.S.T")
        assert isinstance(result, _NoColTable)

    def test_materialise_falls_back_to_table_name_for_disc_style_glob(
        self,
    ) -> None:
        """When ``file_name`` is a disc glob, ``table_name`` wins."""
        engine = _FakeEngine()
        stager = SnowparkEngineStager(engine)
        driver = _FakeDriverWithLocation(
            "snowflake",
            {"database": "D", "schema": "S"},
        )
        stager.materialise(
            driver,
            "demo",
            "20260101",
            "*disc_glob*",
            None,
            table_name="DEMOGRAPHIC",
        )
        assert engine._session.table_calls == ["D.S.DEMOGRAPHIC"]

    def test_materialise_requires_database(self) -> None:
        engine = _FakeEngine()
        stager = SnowparkEngineStager(engine)
        driver = _FakeDriverWithLocation("snowflake", {"schema": "S"})
        with pytest.raises(ValueError, match="'database'"):
            stager.materialise(
                driver,
                "demo",
                "20260101",
                "DEMOGRAPHIC",
                None,
                table_name="DEMOGRAPHIC",
            )

    def test_materialise_rejects_glob_and_no_table_name(self) -> None:
        engine = _FakeEngine()
        stager = SnowparkEngineStager(engine)
        driver = _FakeDriverWithLocation(
            "snowflake",
            {"database": "D", "schema": "S"},
        )
        with pytest.raises(ValueError, match="valid Snowflake identifier"):
            stager.materialise(
                driver,
                "demo",
                "20260101",
                "*disc_glob*",
                None,
                table_name=None,
            )

    def test_persist_dispatches_to_native_strategy_for_snowflake(self) -> None:
        """Native Snowpark→Snowflake write goes through ``save_as_table``.

        The pre-Phase-C strategy called :meth:`driver.write` (Spark-only
        ``df.repartition(10).write.format('snowflake')``). Phase C
        rewires to ``df.write.save_as_table`` — the Snowpark-native
        path.
        """
        engine = _FakeEngine()
        stager = SnowparkEngineStager(engine)
        driver = _FakeDriverWithLocation(
            "snowflake",
            {"database": "MYDB", "schema": "MYSCHEMA"},
        )
        df = _FakeSnowparkDataFrame()
        stager.persist(
            df,
            driver,
            "demo",
            "20260101",
            "demographic",
            None,
        )
        # save_as_table is called with the fully-qualified target as a
        # list of identifier parts — [database, schema, TABLE] — so the
        # session's current schema is never switched (which would break
        # embedded shared-view references at plan-execution time).
        assert df.write.save_as_table_calls == [
            (["MYDB", "MYSCHEMA", "DEMOGRAPHIC"], {"mode": "overwrite"}),
        ]
        # driver.write must NOT be called under the native strategy.
        assert driver.write_calls == []

    def test_persist_does_not_switch_session_schema(self) -> None:
        """The native write must leave the session's DB/schema unchanged.

        The pipeline registers the OMOP ``concept`` /
        ``concept_relationship`` shared views once at startup as
        session-scoped *unqualified* temp views in the workspace schema
        bound by ``SnowparkEngine.prepare_workspace``. A produced
        DataFrame's lazy plan can reference those temp views; because
        ``save_as_table`` executes that plan, switching the session to
        ``USE SCHEMA <output_schema>`` first would make the embedded
        ``session.table("concept")`` resolve against the output schema at
        execution time and fail with ``Object 'CONCEPT' does not exist``
        — the exact pcornet_to_omop failure at persist time. So persist
        must NOT issue ``USE DATABASE`` / ``USE SCHEMA`` at all; it writes
        via a fully-qualified list target instead.
        """
        engine = _FakeEngine()
        session = _ContextTrackingSession(
            db="STAGING_DB",
            schema="OVID2_SNOWPARK_STAGING",
        )
        engine._session = session
        stager = SnowparkEngineStager(engine)
        driver = _FakeDriverWithLocation(
            "snowflake",
            {"database": "OUT_DB", "schema": "OUT_SCHEMA"},
        )
        df = _FakeSnowparkDataFrame()
        stager.persist(df, driver, "demo", "20260101", "person", None)
        # The write happened, fully-qualified, no schema switch.
        assert df.write.save_as_table_calls == [
            (["OUT_DB", "OUT_SCHEMA", "PERSON"], {"mode": "overwrite"}),
        ]
        # The session's current namespace is unchanged, so a following
        # ``session.table("concept")`` still resolves in the workspace.
        assert session.current_database == "STAGING_DB"
        assert session.current_schema == "OVID2_SNOWPARK_STAGING"
        # No USE SCHEMA / USE DATABASE was ever issued.
        assert not any(q.strip().upper().startswith("USE SCHEMA") for q in session.sql_calls)
        assert not any(q.strip().upper().startswith("USE DATABASE") for q in session.sql_calls)
        # It did ensure the target schema exists (idempotent DDL).
        assert any("CREATE SCHEMA IF NOT EXISTS OUT_DB.OUT_SCHEMA" in q for q in session.sql_calls)

    def test_materialise_raises_for_unsupported_driver(self) -> None:
        engine = _FakeEngine()
        stager = SnowparkEngineStager(engine)
        # Use postgres — a driver type not yet supported post-Phase-E.
        driver = _FakeDriver("postgres")
        with pytest.raises(EngineStagerUnsupported) as exc_info:
            stager.materialise(driver, "demo", "20260101", "*ENC*")
        assert exc_info.value.engine_family == "snowpark"
        assert exc_info.value.driver_type == "postgres"

    def test_persist_raises_for_unsupported_driver(self) -> None:
        engine = _FakeEngine()
        stager = SnowparkEngineStager(engine)
        # mssql — driver type not yet supported post-Phase-E.
        driver = _FakeDriver("mssql")
        with pytest.raises(EngineStagerUnsupported) as exc_info:
            stager.persist("df", driver, "demo", "20260101", "ENCOUNTER")
        assert exc_info.value.driver_type == "mssql"

    def test_family_marker(self) -> None:
        assert SnowparkEngineStager.family == "snowpark"

    def test_persist_applies_cdm_types_and_lenient_casts(
        self,
        monkeypatch,
    ) -> None:
        """A CDM-stamped target is created typed with lenient casts.

        Guards the ``Numeric value '' is not recognized`` failure the
        pcornet_to_omop measurement / observation / drug_exposure /
        procedure_occurrence tables hit: the produced DataFrame is
        all-string (``lit("")`` for unset numerics), so writing it into a
        typed OMOP ``NUMBER`` column made Snowflake abort. The native
        strategy now resolves the output CDM's column types and emits a
        ``CREATE OR REPLACE TABLE … AS SELECT`` with ``TRY_CAST(NULLIF(…,
        ''))`` on numeric/temporal columns — empty / malformed values
        become NULL instead of aborting the write.
        """
        # Patch ``_resolve_typed_columns`` in the exact namespace the
        # imported strategy class's ``persist`` executes in. A sibling
        # contract test (``test_snowpark_strategies_registered.py``)
        # deletes ``ovid2.core.engines.stager`` from ``sys.modules`` and
        # reloads it, so re-importing the module by name can hand back a
        # *newer* module object than the one this file's top-level import
        # bound the class to. ``persist`` reads the name from its own
        # ``__globals__``, so patching that dict item is reload-safe.
        strategy_globals = SnowparkEngineStager._STRATEGIES["snowflake"].persist.__globals__
        monkeypatch.setitem(
            strategy_globals,
            "_resolve_typed_columns",
            lambda driver, table_name: [
                ("MEASUREMENT_ID", "NUMBER(38,10)"),
                ("MEASUREMENT_DATE", "DATE"),
                ("VALUE_SOURCE_VALUE", "VARCHAR(250)"),
                ("VALUE_AS_CONCEPT_ID", "NUMBER(38,10)"),
                ("MISSING_COL", "NUMBER(38,10)"),
            ],
        )

        engine = _FakeEngine()
        session = _ContextTrackingSession(db="OUT_DB", schema="OUT_SCHEMA")
        engine._session = session
        stager = SnowparkEngineStager(engine)
        driver = _FakeDriverWithLocation(
            "snowflake",
            {"database": "OUT_DB", "schema": "OUT_SCHEMA", "cdm": "omop_5_3"},
        )
        df = _FakeSnowparkDataFrame(
            columns=[
                "MEASUREMENT_ID",
                "MEASUREMENT_DATE",
                "VALUE_SOURCE_VALUE",
                "VALUE_AS_CONCEPT_ID",
            ],
        )
        stager.persist(df, driver, "demo", "20260101", "measurement", None)

        # A temp view was registered for the CTAS to read from.
        assert df.temp_views, "expected a staging temp view"
        view = df.temp_views[0]

        ctas = [q for q in session.sql_calls if q.startswith("CREATE OR REPLACE TABLE")]
        assert len(ctas) == 1
        stmt = ctas[0]
        # Fully-qualified target, no session-schema switch.
        assert "CREATE OR REPLACE TABLE OUT_DB.OUT_SCHEMA.MEASUREMENT AS" in stmt
        assert f"FROM {view}" in stmt
        # Numeric column: lenient TRY_CAST with empty→NULL coercion.
        assert (
            "TRY_CAST(NULLIF(TRIM(TO_VARCHAR(\"MEASUREMENT_ID\")), '') "
            'AS NUMBER(38,10)) AS "MEASUREMENT_ID"'
        ) in stmt
        # Date column also lenient.
        assert 'AS DATE) AS "MEASUREMENT_DATE"' in stmt
        # VARCHAR is width-truncated with LEFT so over-long source values
        # land truncated instead of aborting the write.
        assert ('CAST(LEFT(TO_VARCHAR("VALUE_SOURCE_VALUE"), 250) AS VARCHAR(250))') in stmt
        # A CDM column absent from the DataFrame becomes a typed NULL.
        assert 'CAST(NULL AS NUMBER(38,10)) AS "MISSING_COL"' in stmt
        # No USE SCHEMA / USE DATABASE issued.
        assert not any(
            q.strip().upper().startswith(("USE SCHEMA", "USE DATABASE")) for q in session.sql_calls
        )
        # save_as_table must NOT be used on the typed path.
        assert df.write.save_as_table_calls == []

    def test_persist_truncates_over_width_varchar_columns(
        self,
        monkeypatch,
    ) -> None:
        """Width-bounded string columns are truncated, not hard-cast.

        Guards the ``String '...' is too long and would be truncated``
        (100078 / 22000) failure the pcornet_to_omop ``observation``
        (``VALUE_AS_STRING`` VARCHAR(250)) and ``specimen``
        (``SPECIMEN_SOURCE_VALUE`` VARCHAR(50)) tables hit: a plain
        ``CAST(... AS VARCHAR(N))`` makes Snowflake *reject* any value
        longer than ``N``, aborting the whole write. The legacy
        Spark→Snowflake connector silently truncated over-width strings;
        the typed writer now mirrors that with an explicit ``LEFT``.

        Also exercises the ``TEXT(n)`` / ``STRING(n)`` / bare-``VARCHAR``
        spellings so the width parser stays robust.
        """
        strategy_globals = SnowparkEngineStager._STRATEGIES["snowflake"].persist.__globals__
        monkeypatch.setitem(
            strategy_globals,
            "_resolve_typed_columns",
            lambda driver, table_name: [
                ("SPECIMEN_SOURCE_VALUE", "VARCHAR(50)"),
                ("VALUE_AS_STRING", "TEXT(250)"),
                ("NAME", "STRING(20)"),
                ("UNBOUNDED", "VARCHAR"),
            ],
        )

        engine = _FakeEngine()
        session = _ContextTrackingSession(db="OUT_DB", schema="OUT_SCHEMA")
        engine._session = session
        stager = SnowparkEngineStager(engine)
        driver = _FakeDriverWithLocation(
            "snowflake",
            {"database": "OUT_DB", "schema": "OUT_SCHEMA", "cdm": "omop_5_3"},
        )
        df = _FakeSnowparkDataFrame(
            columns=[
                "SPECIMEN_SOURCE_VALUE",
                "VALUE_AS_STRING",
                "NAME",
                "UNBOUNDED",
            ],
        )
        stager.persist(df, driver, "demo", "20260101", "specimen", None)

        ctas = [q for q in session.sql_calls if q.startswith("CREATE OR REPLACE TABLE")]
        assert len(ctas) == 1
        stmt = ctas[0]
        # Width-bounded VARCHAR / TEXT / STRING columns get a truncating LEFT.
        assert ('CAST(LEFT(TO_VARCHAR("SPECIMEN_SOURCE_VALUE"), 50) AS VARCHAR(50))') in stmt
        assert ('CAST(LEFT(TO_VARCHAR("VALUE_AS_STRING"), 250) AS TEXT(250))') in stmt
        assert 'CAST(LEFT(TO_VARCHAR("NAME"), 20) AS STRING(20))' in stmt
        # An unbounded VARCHAR (no width) keeps the plain CAST.
        assert 'CAST("UNBOUNDED" AS VARCHAR) AS "UNBOUNDED"' in stmt


# ---------------------------------------------------------------------------
# register_snowpark_strategy
# ---------------------------------------------------------------------------


class _FakeDiscStrategy(SnowparkStagingStrategy):
    """Illustrative future strategy — used to prove registration works.

    Uses a synthetic ``driver_type`` ("_test_stagable") so registration
    tests don't collide with the real Phase-C ``SnowparkDiscStrategy``
    (which self-registers on the ``disc`` slot at driver-package
    import time).
    """

    driver_type = "_test_stagable"

    def __init__(self) -> None:
        self.materialise_calls: list[Any] = []
        self.persist_calls: list[Any] = []

    def materialise(
        self,
        session: Any,
        driver: Any,
        pipeline: str,
        batch_id: str,
        file_name: str,
        delimiter: str | None,
        table_name: str | None,
    ) -> str:
        self.materialise_calls.append(
            (session, driver, pipeline, batch_id, file_name, delimiter, table_name),
        )
        return "staged_disc_df"

    def persist(
        self,
        session: Any,
        df: Any,
        driver: Any,
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str | None,
    ) -> None:
        self.persist_calls.append(
            (session, df, driver, pipeline, batch_id, table_name, delimiter),
        )


class TestRegisterSnowparkStrategy:
    def test_registered_type_becomes_supported(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Isolate the strategy map so this test doesn't leak into
        # sibling tests (they assert Phase A+B/C state).
        monkeypatch.setattr(
            SnowparkEngineStager,
            "_STRATEGIES",
            dict(SnowparkEngineStager._STRATEGIES),
        )
        assert SnowparkEngineStager.supports("_test_stagable") is False
        register_snowpark_strategy(_FakeDiscStrategy)
        assert SnowparkEngineStager.supports("_test_stagable") is True

    def test_registered_strategy_is_used_for_materialise(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            SnowparkEngineStager,
            "_STRATEGIES",
            dict(SnowparkEngineStager._STRATEGIES),
        )
        register_snowpark_strategy(_FakeDiscStrategy)

        engine = _FakeEngine()
        stager = SnowparkEngineStager(engine)
        driver = _FakeDriver("_test_stagable")
        df = stager.materialise(
            driver,
            "demo",
            "20260101",
            "*ENC*",
            None,
            table_name="ENCOUNTER",
        )
        # Uses the registered strategy, not the pass-through.
        assert df == "staged_disc_df"
        # And the driver's ``read`` was not called directly.
        assert driver.read_calls == []

    def test_missing_driver_type_raises(self) -> None:
        class _NoTypeStrategy(SnowparkStagingStrategy):
            driver_type = ""

            def materialise(self, *a: Any, **kw: Any) -> Any:  # pragma: no cover
                return None

            def persist(self, *a: Any, **kw: Any) -> None:  # pragma: no cover
                return None

        with pytest.raises(ValueError, match="driver_type"):
            register_snowpark_strategy(_NoTypeStrategy)


# ---------------------------------------------------------------------------
# Family / type-name helpers
# ---------------------------------------------------------------------------


class TestStagerLookupHelpers:
    def test_spark_family_returns_spark_stager(self) -> None:
        assert stager_class_for_engine_family("spark") is SparkEngineStager

    def test_snowpark_family_returns_snowpark_stager(self) -> None:
        assert stager_class_for_engine_family("snowpark") is SnowparkEngineStager

    def test_unknown_family_returns_none(self) -> None:
        assert stager_class_for_engine_family("no_such_family") is None

    def test_type_name_pyspark_resolves_to_spark_stager(self) -> None:
        assert stager_class_for_engine_type("pyspark") is SparkEngineStager

    def test_type_name_databricks_spark_resolves_to_spark_stager(self) -> None:
        assert stager_class_for_engine_type("databricks_spark") is SparkEngineStager

    def test_type_name_snowpark_resolves_to_snowpark_stager(self) -> None:
        assert stager_class_for_engine_type("snowpark") is SnowparkEngineStager

    def test_unknown_type_name_returns_none(self) -> None:
        """Named-config engine names (not bare types) are unknown."""
        assert stager_class_for_engine_type("my-prod-engine") is None


# ---------------------------------------------------------------------------
# EngineStager.session
# ---------------------------------------------------------------------------


class TestSessionAccessor:
    def test_session_delegates_to_engine(self) -> None:
        engine = _FakeEngine()
        stager = SparkEngineStager(engine)
        s = stager.session()
        assert s is engine._session
        assert engine.get_session_calls == 1


# ---------------------------------------------------------------------------
# EngineStager is abstract
# ---------------------------------------------------------------------------


class TestAbstract:
    def test_cannot_instantiate_base_class(self) -> None:
        with pytest.raises(TypeError):
            EngineStager(_FakeEngine())  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# Native Snowpark strategy: ``path``-form Snowflake locations
# ---------------------------------------------------------------------------


class TestNativeSnowparkPathLocation:
    """``{"path": "DB[/SCHEMA]"}`` addresses work the same as explicit keys.

    Regression: the web UI saves Snowflake addresses as
    ``{"path": "CHAR_DEMO/PUBLIC"}``; the native strategy only read
    ``location.database`` and failed with "driver has no 'database'"
    on every Snowpark copy from such an address.
    """

    def test_materialise_reads_database_and_schema_from_path(self) -> None:
        engine = _FakeEngine()
        stager = SnowparkEngineStager(engine)
        driver = _FakeDriverWithLocation("snowflake", {"path": "CHAR_DEMO/PUBLIC"})
        stager.materialise(driver, "demo", "b1", "DEMOGRAPHIC", None, table_name="DEMOGRAPHIC")
        assert engine._session.table_calls == ["CHAR_DEMO.PUBLIC.DEMOGRAPHIC"]

    def test_explicit_keys_win_over_path(self) -> None:
        engine = _FakeEngine()
        stager = SnowparkEngineStager(engine)
        driver = _FakeDriverWithLocation(
            "snowflake", {"path": "IGNORED/X", "database": "D", "schema": "S"}
        )
        stager.materialise(driver, "demo", "b1", "T", None, table_name="T")
        assert engine._session.table_calls == ["D.S.T"]

    def test_persist_path_with_schema_targets_it_verbatim(self) -> None:
        engine = _FakeEngine()
        stager = SnowparkEngineStager(engine)
        driver = _FakeDriverWithLocation("snowflake", {"path": "SANDBOX/PUBLIC"})
        df = _FakeSnowparkDataFrame()
        stager.persist(df, driver, "demo", "b1", "demographic", None)
        assert df.write.save_as_table_calls == [
            (["SANDBOX", "PUBLIC", "DEMOGRAPHIC"], {"mode": "overwrite"}),
        ]

    def test_persist_database_only_path_is_rejected(self) -> None:
        """``{"path": "SANDBOX"}`` names no schema → rejected (no ``OVID_*`` fallback)."""
        engine = _FakeEngine()
        stager = SnowparkEngineStager(engine)
        driver = _FakeDriverWithLocation("snowflake", {"path": "SANDBOX"})
        df = _FakeSnowparkDataFrame()
        with pytest.raises(ValueError, match="DATABASE/SCHEMA"):
            stager.persist(df, driver, "COPY_SNOWPARK", "20261023", "demographic", None)
        assert df.write.save_as_table_calls == []

    def test_persist_date_shaped_schema_is_quoted(self) -> None:
        """A date-shaped schema (``SANDBOX/20261023``) is double-quoted in DDL."""
        engine = _FakeEngine()
        stager = SnowparkEngineStager(engine)
        driver = _FakeDriverWithLocation("snowflake", {"path": "SANDBOX/20261023"})
        df = _FakeSnowparkDataFrame()
        stager.persist(df, driver, "demo", "SANDBOX/20261023", "demographic", None)
        assert 'CREATE SCHEMA IF NOT EXISTS SANDBOX."20261023"' in engine._session.sql_calls
