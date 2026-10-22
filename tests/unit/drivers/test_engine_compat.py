"""Unit tests for :mod:`ovid2.drivers.engine_compat`.

Covers the plan-time engine/driver family compatibility check used by
``ovid2 tools copy`` and ``ovid2 tools move`` to refuse an incompatible
pairing before any Spark cluster starts.

Regression scope: closes the crash reported at
``ovid2 tools copy --from CHAR_ACME --to S3-Testing --engine snowpark``
where the container used to fail deep inside
``SnowparkEngine.get_session`` with
``ModuleNotFoundError: No module named 'snowflake.snowpark'``. The
underlying error was semantic — Snowpark DataFrames live inside
Snowflake and cannot write to arbitrary ``s3a://`` — so the check
here refuses the pairing on driver-metadata grounds regardless of
whether the Snowpark package is importable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ovid2.drivers.engine_compat import (
    _driver_class_for_address,
    assert_engine_supports_drivers,
)

# ---------------------------------------------------------------------------
# Fixtures — mirror tests/unit/drivers/test_type_stamping.py so the
# fixture shape is familiar across the module.
# ---------------------------------------------------------------------------


@pytest.fixture
def config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point OVID2's config loaders at an isolated tmp dir."""
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv(
        "OVID2_CONFIGS_DIR",
        str(xdg / "ovid2" / "configs"),
    )
    monkeypatch.setenv(
        "OVID2_CONNECTIONS_DIR",
        str(xdg / "ovid2" / "connections"),
    )
    (xdg / "ovid2" / "configs").mkdir(parents=True)
    (xdg / "ovid2" / "connections").mkdir(parents=True)
    return xdg


def _conn(xdg: Path, name: str, payload: dict[str, Any]) -> None:
    (xdg / "ovid2" / "connections" / f"{name}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _addr(xdg: Path, name: str, payload: dict[str, Any]) -> None:
    (xdg / "ovid2" / "configs" / f"{name}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Stubs — avoid instantiating the real engines (which for Snowpark would
# force ``snowflake.snowpark`` into ``sys.modules``, defeating the
# import-safety of the check).
# ---------------------------------------------------------------------------


class _StubEngine:
    """Minimal stand-in for a :class:`BaseEngine`.

    ``assert_engine_supports_drivers`` reads ``family``, ``type_name``,
    and calls ``get_stager()``. The stub returns a stager instance
    built from the real family-map so the check exercises the same
    code path a real engine would, but without importing pyspark /
    snowpark.
    """

    def __init__(self, family_value: str, type_name: str) -> None:
        # The real engines expose ``family`` as an :class:`EngineFamily`
        # enum instance; the helper also accepts a plain string. Cover
        # both paths.
        from ovid2.core.engines.protocols import EngineFamily

        self.family = EngineFamily(family_value)
        self.type_name = type_name

    def get_stager(self) -> Any:
        from ovid2.core.engines.stager import stager_class_for_engine_family

        cls = stager_class_for_engine_family(self.family.value)
        assert cls is not None, f"no stager for family {self.family.value!r}"
        return cls(self)


class _StringFamilyEngine:
    """Same as :class:`_StubEngine` but with a bare-string ``family``.

    Exercises the ``isinstance(engine.family, EngineFamily)`` false
    branch of the helper.
    """

    def __init__(self, family_str: str, type_name: str) -> None:
        self.family = family_str
        self.type_name = type_name

    def get_stager(self) -> Any:
        from ovid2.core.engines.stager import stager_class_for_engine_family

        cls = stager_class_for_engine_family(str(self.family))
        assert cls is not None, f"no stager for family {self.family!r}"
        return cls(self)


# ---------------------------------------------------------------------------
# _driver_class_for_address
# ---------------------------------------------------------------------------


class TestDriverClassForAddress:
    """Exercises the private helper that resolves an address to a class."""

    def test_missing_address_returns_none(self, config_env: Path) -> None:
        assert _driver_class_for_address("nope") is None

    def test_malformed_json_returns_none(self, config_env: Path) -> None:
        (config_env / "ovid2" / "configs" / "bad.json").write_text(
            "{not json",
            encoding="utf-8",
        )
        assert _driver_class_for_address("bad") is None

    def test_w7_disc_resolves(self, config_env: Path, tmp_path: Path) -> None:
        host = tmp_path / "d"
        host.mkdir()
        _conn(
            config_env,
            "d_conn",
            {
                "type": "disc",
                "args": {"host_path": str(host)},
            },
        )
        _addr(config_env, "a", {"connection": "d_conn", "location": {}})
        cls = _driver_class_for_address("a")
        assert cls is not None
        assert getattr(cls, "type_name", "") == "disc"

    def test_w7_snowflake_resolves(self, config_env: Path) -> None:
        _conn(
            config_env,
            "sf",
            {
                "type": "snowflake",
                "args": {"account": "acct", "user": "u"},
            },
        )
        _addr(
            config_env,
            "sf_addr",
            {
                "connection": "sf",
                "location": {"database": "DB"},
            },
        )
        cls = _driver_class_for_address("sf_addr")
        assert cls is not None
        assert getattr(cls, "type_name", "") == "snowflake"

    def test_legacy_s3_resolves(self, config_env: Path) -> None:
        _addr(
            config_env,
            "s3_legacy",
            {
                "src_name": "s3",
                "src_args": {"bucket": "b"},
            },
        )
        cls = _driver_class_for_address("s3_legacy")
        assert cls is not None
        assert getattr(cls, "type_name", "") == "s3"

    def test_unregistered_type_returns_none(self, config_env: Path) -> None:
        _addr(
            config_env,
            "unk",
            {
                "src_name": "nonexistent_type",
                "src_args": {},
            },
        )
        assert _driver_class_for_address("unk") is None

    def test_empty_connection_field_returns_none(self, config_env: Path) -> None:
        _addr(config_env, "bad", {"connection": "", "location": {}})
        assert _driver_class_for_address("bad") is None


# ---------------------------------------------------------------------------
# assert_engine_supports_drivers — happy paths
# ---------------------------------------------------------------------------


class TestAcceptedPairings:
    """Pairings that should proceed silently."""

    def test_pyspark_with_disc_disc(
        self,
        config_env: Path,
        tmp_path: Path,
    ) -> None:
        host = tmp_path / "d"
        host.mkdir()
        _conn(
            config_env,
            "d",
            {
                "type": "disc",
                "args": {"host_path": str(host)},
            },
        )
        _addr(config_env, "src", {"connection": "d", "location": {}})
        _addr(config_env, "dst", {"connection": "d", "location": {}})
        engine = _StubEngine("spark", "pyspark")
        # Must not raise.
        assert assert_engine_supports_drivers(engine, ["src", "dst"]) is None

    def test_pyspark_with_snowflake_s3(self, config_env: Path) -> None:
        _conn(
            config_env,
            "sf",
            {
                "type": "snowflake",
                "args": {"account": "acct", "user": "u"},
            },
        )
        _addr(
            config_env,
            "src",
            {
                "connection": "sf",
                "location": {"database": "DB"},
            },
        )
        _addr(
            config_env,
            "dst",
            {
                "src_name": "s3",
                "src_args": {"bucket": "b"},
            },
        )
        engine = _StubEngine("spark", "pyspark")
        assert assert_engine_supports_drivers(engine, ["src", "dst"]) is None

    def test_snowpark_with_snowflake_snowflake(
        self,
        config_env: Path,
    ) -> None:
        """Both endpoints Snowflake → snowpark accepted."""
        _conn(
            config_env,
            "sf",
            {
                "type": "snowflake",
                "args": {"account": "acct", "user": "u"},
            },
        )
        _addr(
            config_env,
            "src",
            {
                "connection": "sf",
                "location": {"database": "S"},
            },
        )
        _addr(
            config_env,
            "dst",
            {
                "connection": "sf",
                "location": {"database": "T"},
            },
        )
        engine = _StubEngine("snowpark", "snowpark")
        assert assert_engine_supports_drivers(engine, ["src", "dst"]) is None

    def test_databricks_spark_with_azure_blob(
        self,
        config_env: Path,
    ) -> None:
        """Databricks-Spark is a Spark family engine → accepted with any driver."""
        _conn(
            config_env,
            "az",
            {
                "type": "azure_blob",
                "args": {"account_name": "acct"},
            },
        )
        _addr(
            config_env,
            "src",
            {
                "connection": "az",
                "location": {"container": "c"},
            },
        )
        engine = _StubEngine("spark", "databricks_spark")
        assert assert_engine_supports_drivers(engine, ["src"]) is None

    def test_engine_with_bare_string_family_accepted(
        self,
        config_env: Path,
        tmp_path: Path,
    ) -> None:
        """The helper reads ``str`` and :class:`EngineFamily` uniformly."""
        host = tmp_path / "d"
        host.mkdir()
        _conn(
            config_env,
            "d",
            {
                "type": "disc",
                "args": {"host_path": str(host)},
            },
        )
        _addr(config_env, "src", {"connection": "d", "location": {}})
        engine = _StringFamilyEngine("spark", "pyspark")
        assert assert_engine_supports_drivers(engine, ["src"]) is None


# ---------------------------------------------------------------------------
# assert_engine_supports_drivers — rejection path
# ---------------------------------------------------------------------------


class TestRejectedPairings:
    """The failure modes that motivated this module."""

    def test_snowpark_with_s3_destination_accepted_since_phase_d(
        self,
        config_env: Path,
    ) -> None:
        """Phase D landed :class:`SnowparkS3Strategy`.

        The exact CHAR_ACME → S3-Testing crash from the pre-Phase-D
        incident (which motivated the entire orthogonality refactor)
        is now a supported pairing. The strategy CREATE STAGEs the
        target S3 URL and Snowflake writes directly into the bucket
        via ``df.write.copy_into_location`` — no download step, no
        cross-peripheral bridge through the client. See
        the adr-engine-storage-orthogonality design note.
        """
        _conn(
            config_env,
            "sf",
            {
                "type": "snowflake",
                "args": {"account": "acct", "user": "u"},
            },
        )
        _addr(
            config_env,
            "CHAR_ACME",
            {
                "connection": "sf",
                "location": {"database": "CHAR_ACME", "schema": "PUBLIC"},
            },
        )
        _conn(
            config_env,
            "s3_conn",
            {
                "type": "s3",
                "args": {"region": "us-east-2"},
            },
        )
        _addr(
            config_env,
            "S3-Testing",
            {
                "connection": "s3_conn",
                "location": {"bucket": "b"},
            },
        )
        engine = _StubEngine("snowpark", "snowpark")
        # No exception — the pair is compatible post-Phase-D.
        assert_engine_supports_drivers(
            engine,
            ["CHAR_ACME", "S3-Testing"],
        )

    def test_snowpark_with_postgres_rejected(
        self,
        config_env: Path,
    ) -> None:
        """Postgres is still not reachable under --engine snowpark.

        Serves as the residual "still-rejected pairing" test now that
        S3, Azure Blob, and Databricks are supported (post-Phase-E).
        No strategy for the postgres driver exists on the Snowpark
        stager. Postgres is the natural next candidate for a
        Phase-F-style follow-up.
        """
        _conn(
            config_env,
            "sf",
            {
                "type": "snowflake",
                "args": {"account": "acct", "user": "u"},
            },
        )
        _addr(
            config_env,
            "sf_addr",
            {
                "connection": "sf",
                "location": {"database": "T", "schema": "PUBLIC"},
            },
        )
        _conn(
            config_env,
            "pg_conn",
            {
                "type": "postgres",
                "args": {"host": "db.example.com", "database": "d", "user": "u"},
            },
        )
        _addr(
            config_env,
            "pg_addr",
            {
                "connection": "pg_conn",
                "location": {"database": "d", "schema": "public"},
            },
        )
        engine = _StubEngine("snowpark", "snowpark")
        with pytest.raises(RuntimeError) as excinfo:
            assert_engine_supports_drivers(
                engine,
                ["sf_addr", "pg_addr"],
            )
        msg = str(excinfo.value)
        assert "pg_addr" in msg
        assert "snowpark" in msg
        assert "postgres" in msg

    def test_snowpark_with_disc_source_accepted_since_phase_c(
        self,
        config_env: Path,
        tmp_path: Path,
    ) -> None:
        """Phase C landed a ``SnowparkDiscStrategy`` (disc via ``@~`` stage).

        Snowpark + disc endpoints are now a supported pairing. The
        pre-Phase-C version of this test asserted the pair was
        rejected; that contract flipped when
        :mod:`ovid2.drivers.snowflake.staging.disc` self-registered
        on the ``SnowparkEngineStager`` dispatch map.
        """
        host = tmp_path / "d"
        host.mkdir()
        _conn(
            config_env,
            "d",
            {
                "type": "disc",
                "args": {"host_path": str(host)},
            },
        )
        _addr(config_env, "src", {"connection": "d", "location": {}})
        _conn(
            config_env,
            "sf",
            {
                "type": "snowflake",
                "args": {"account": "acct", "user": "u"},
            },
        )
        _addr(
            config_env,
            "dst",
            {
                "connection": "sf",
                "location": {"database": "T"},
            },
        )
        engine = _StubEngine("snowpark", "snowpark")
        # No exception — the pair is compatible.
        assert_engine_supports_drivers(engine, ["src", "dst"])

    def test_snowpark_with_azure_blob_accepted_since_phase_d(
        self,
        config_env: Path,
    ) -> None:
        """Phase D landed :class:`SnowparkAzureBlobStrategy`.

        Snowpark + azure_blob endpoints are now a supported pairing
        through the external-stage machinery. Auth path selection
        (storage integration vs SAS vs account key) is documented in
        the strategy module.
        """
        _conn(
            config_env,
            "az",
            {
                "type": "azure_blob",
                "args": {"account_name": "acct"},
            },
        )
        _addr(
            config_env,
            "src",
            {
                "connection": "az",
                "location": {"container": "c"},
            },
        )
        engine = _StubEngine("snowpark", "snowpark")
        assert_engine_supports_drivers(engine, ["src"])

    def test_snowpark_with_databricks_accepted_since_phase_e(
        self,
        config_env: Path,
    ) -> None:
        """Phase E landed :class:`SnowparkDatabricksStrategy`.

        Snowpark + databricks endpoints are now a supported pairing
        through the S3-bounce machinery. Auth path resolution
        (storage integration vs inline AWS creds on the bounce
        bucket) is documented in the strategy module.
        """
        _conn(
            config_env,
            "db_conn",
            {
                "type": "databricks",
                "args": {"host": "example.databricks.com"},
            },
        )
        _addr(
            config_env,
            "src",
            {
                "connection": "db_conn",
                "location": {"catalog": "main", "schema": "s"},
            },
        )
        engine = _StubEngine("snowpark", "snowpark")
        assert_engine_supports_drivers(engine, ["src"])

    def test_error_names_both_engine_and_driver(
        self,
        config_env: Path,
        tmp_path: Path,
    ) -> None:
        # Uses postgres as the still-rejected driver type post-Phase-E
        # (disc/s3/azure_blob/databricks are now compatible via their
        # respective SnowparkStagingStrategies).
        _conn(
            config_env,
            "pg_conn",
            {
                "type": "postgres",
                "args": {"host": "db.example.com", "database": "d", "user": "u"},
            },
        )
        _addr(
            config_env,
            "src",
            {
                "connection": "pg_conn",
                "location": {"database": "d", "schema": "public"},
            },
        )
        engine = _StubEngine("snowpark", "snowpark_prod")
        with pytest.raises(RuntimeError) as excinfo:
            assert_engine_supports_drivers(engine, ["src"])
        msg = str(excinfo.value)
        assert "snowpark_prod" in msg
        assert "postgres" in msg
        # Supported families should be listed for the driver.
        assert "spark" in msg


# ---------------------------------------------------------------------------
# assert_engine_supports_drivers — skip paths (unresolvable addresses)
# ---------------------------------------------------------------------------


class TestSkipPaths:
    """Addresses whose driver cannot be resolved should not block the run."""

    def test_missing_address_is_skipped(self, config_env: Path) -> None:
        engine = _StubEngine("snowpark", "snowpark")
        # No config for 'nope' → helper returns silently.
        assert assert_engine_supports_drivers(engine, ["nope"]) is None

    def test_unregistered_type_is_skipped(self, config_env: Path) -> None:
        _addr(
            config_env,
            "unk",
            {
                "src_name": "made_up_type",
                "src_args": {},
            },
        )
        engine = _StubEngine("snowpark", "snowpark")
        assert assert_engine_supports_drivers(engine, ["unk"]) is None

    def test_empty_address_list_is_ok(self) -> None:
        engine = _StubEngine("snowpark", "snowpark")
        assert assert_engine_supports_drivers(engine, []) is None


# ---------------------------------------------------------------------------
# Stager-based capability discovery — Phase A+B replaced the driver-side
# ``engine_families`` classmethod with :class:`EngineStager.supports`.
# These tests lock in the same fact from the new angle: Snowpark
# accepts Snowflake and rejects every other driver today; Spark accepts
# everything.
# ---------------------------------------------------------------------------


class TestStagerCapabilityDiscovery:
    def test_snowpark_stager_supports_snowflake(self) -> None:
        from ovid2.core.engines.stager import SnowparkEngineStager

        assert SnowparkEngineStager.supports("snowflake") is True

    def test_snowpark_stager_supports_disc_since_phase_c(self) -> None:
        """Phase C landed :class:`SnowparkDiscStrategy` for the disc driver.

        The strategy self-registers on the
        ``SnowparkEngineStager._STRATEGIES`` dispatch map at
        :mod:`ovid2.drivers.snowflake` import time. Once it's there,
        the compat check accepts ``--engine snowpark`` + disc
        endpoints.
        """
        # Ensure the strategy is registered (triggered by driver import).
        import ovid2.drivers.snowflake  # noqa: F401
        from ovid2.core.engines.stager import SnowparkEngineStager

        assert SnowparkEngineStager.supports("disc") is True

    def test_snowpark_stager_supports_s3_and_azure_since_phase_d(self) -> None:
        """Phase D landed :class:`SnowparkS3Strategy` and
        :class:`SnowparkAzureBlobStrategy`.

        Both self-register on the ``SnowparkEngineStager._STRATEGIES``
        dispatch map at :mod:`ovid2.drivers.snowflake` import time.
        The compat check now accepts ``--engine snowpark`` + s3 or
        azure_blob endpoints.
        """
        # Ensure the strategies are registered.
        import ovid2.drivers.snowflake  # noqa: F401
        from ovid2.core.engines.stager import SnowparkEngineStager

        assert SnowparkEngineStager.supports("s3") is True
        assert SnowparkEngineStager.supports("azure_blob") is True

    def test_snowpark_stager_supports_databricks_since_phase_e(self) -> None:
        """Phase E landed :class:`SnowparkDatabricksStrategy`.

        Self-registers on the ``SnowparkEngineStager._STRATEGIES``
        dispatch map at :mod:`ovid2.drivers.snowflake` import time.
        The compat check now accepts ``--engine snowpark`` + databricks
        endpoints (routed via an S3 bounce).
        """
        import ovid2.drivers.snowflake  # noqa: F401
        from ovid2.core.engines.stager import SnowparkEngineStager

        assert SnowparkEngineStager.supports("databricks") is True

    def test_snowpark_stager_rejects_postgres_today(self) -> None:
        """Post-Phase-F residual — postgres/mssql strategies not yet landed."""
        from ovid2.core.engines.stager import SnowparkEngineStager

        assert SnowparkEngineStager.supports("postgres") is False
        assert SnowparkEngineStager.supports("mssql") is False

    def test_snowpark_stager_supports_gcs_since_phase_f(self) -> None:
        """Phase F landed :class:`SnowparkGcsStrategy`.

        Self-registers on the ``SnowparkEngineStager._STRATEGIES``
        dispatch map at :mod:`ovid2.drivers.snowflake` import time.
        The compat check now accepts ``--engine snowpark`` + gcs
        endpoints (auth path is integration-only — see the strategy
        module).
        """
        import ovid2.drivers.snowflake  # noqa: F401
        from ovid2.core.engines.stager import SnowparkEngineStager

        assert SnowparkEngineStager.supports("gcs") is True

    def test_spark_stager_supports_every_registered_driver(self) -> None:
        from ovid2.core.engines.stager import SparkEngineStager
        from ovid2.drivers.registry import DRIVERS

        for driver_type in sorted(DRIVERS):
            assert SparkEngineStager.supports(driver_type) is True, driver_type


# ---------------------------------------------------------------------------
# assert_engine_type_supports_drivers — the web-safe type-name variant
# used by ``ovid2.web.services.jobs_svc`` (which cannot import
# ``ovid2.core.engines.*``) and by ``ovid2.cli.etl`` (where deferring
# the ``load_engine`` call lets the check fire before the engine is
# constructed).
# ---------------------------------------------------------------------------


class TestAssertEngineTypeSupportsDrivers:
    """Type-name variant — no BaseEngine instance required."""

    def test_snowpark_accepted_with_disc_source_since_phase_c(
        self,
        config_env: Path,
        tmp_path: Path,
    ) -> None:
        """Phase C landed :class:`SnowparkDiscStrategy` (disc via ``@~``).

        The compat check now accepts ``--engine snowpark`` +
        ``disc_input`` + ``CHAR_ACME`` — the pipeline handles the
        disc→Snowflake staging via ``session.file.put`` +
        ``session.read.csv('@~/…')`` at ``materialise`` time. See
        ADR the adr-engine-storage-orthogonality design note.

        The pre-Phase-C version of this test asserted rejection with
        an "--engine pyspark" recommendation in the error message;
        that recommendation no longer applies for disc endpoints.
        """
        # Force the disc strategy to register.
        import ovid2.drivers.snowflake  # noqa: F401
        from ovid2.drivers.engine_compat import (
            assert_engine_type_supports_drivers,
        )

        host = tmp_path / "in"
        host.mkdir()
        _conn(
            config_env,
            "d_conn",
            {
                "type": "disc",
                "args": {"host_path": str(host)},
            },
        )
        _addr(
            config_env,
            "disc_input",
            {
                "connection": "d_conn",
                "location": {},
            },
        )
        _addr(
            config_env,
            "CHAR_ACME",
            {
                "src_name": "snowflake",
                "src_args": {"database": "CHAR_ACME", "schema": "PUBLIC"},
            },
        )
        # No exception — the pair is compatible post-Phase-C.
        assert_engine_type_supports_drivers(
            "snowpark",
            ["disc_input", "CHAR_ACME"],
        )

    def test_snowpark_still_rejected_with_postgres_source(
        self,
        config_env: Path,
    ) -> None:
        """Post-Phase-E residual: postgres strategy not yet landed."""
        from ovid2.drivers.engine_compat import (
            assert_engine_type_supports_drivers,
        )

        _conn(
            config_env,
            "pg_conn",
            {
                "type": "postgres",
                "args": {"host": "db.example.com", "database": "d", "user": "u"},
            },
        )
        _addr(
            config_env,
            "pg_input",
            {
                "connection": "pg_conn",
                "location": {"database": "d", "schema": "public"},
            },
        )
        with pytest.raises(RuntimeError) as exc_info:
            assert_engine_type_supports_drivers(
                "snowpark",
                ["pg_input"],
            )
        message = str(exc_info.value)
        assert "snowpark" in message
        assert "pg_input" in message
        assert "--engine pyspark" in message

    def test_pyspark_accepted_with_disc_and_snowflake(
        self,
        config_env: Path,
        tmp_path: Path,
    ) -> None:
        """The compatible fix for the incident above."""
        from ovid2.drivers.engine_compat import (
            assert_engine_type_supports_drivers,
        )

        host = tmp_path / "in"
        host.mkdir()
        _conn(
            config_env,
            "d_conn",
            {
                "type": "disc",
                "args": {"host_path": str(host)},
            },
        )
        _addr(
            config_env,
            "disc_input",
            {
                "connection": "d_conn",
                "location": {},
            },
        )
        _addr(
            config_env,
            "CHAR_ACME",
            {
                "src_name": "snowflake",
                "src_args": {"database": "CHAR_ACME", "schema": "PUBLIC"},
            },
        )
        assert (
            assert_engine_type_supports_drivers(
                "pyspark",
                ["disc_input", "CHAR_ACME"],
            )
            is None
        )

    def test_snowpark_with_two_snowflake_endpoints_accepted(
        self,
        config_env: Path,
    ) -> None:
        """The one legitimate use of ``--engine snowpark``."""
        from ovid2.drivers.engine_compat import (
            assert_engine_type_supports_drivers,
        )

        _addr(
            config_env,
            "sf1",
            {
                "src_name": "snowflake",
                "src_args": {"database": "DB1", "schema": "S"},
            },
        )
        _addr(
            config_env,
            "sf2",
            {
                "src_name": "snowflake",
                "src_args": {"database": "DB2", "schema": "S"},
            },
        )
        assert (
            assert_engine_type_supports_drivers(
                "snowpark",
                ["sf1", "sf2"],
            )
            is None
        )

    def test_unknown_engine_type_is_skipped(
        self,
        config_env: Path,
        tmp_path: Path,
    ) -> None:
        """A named-config engine (not a bare type) is skipped."""
        from ovid2.drivers.engine_compat import (
            assert_engine_type_supports_drivers,
        )

        host = tmp_path / "in"
        host.mkdir()
        _conn(
            config_env,
            "d_conn",
            {
                "type": "disc",
                "args": {"host_path": str(host)},
            },
        )
        _addr(
            config_env,
            "disc_input",
            {
                "connection": "d_conn",
                "location": {},
            },
        )
        # This engine name isn't in the web-safe map → skip. No raise.
        assert (
            assert_engine_type_supports_drivers(
                "my-prod-pyspark",
                ["disc_input"],
            )
            is None
        )

    def test_databricks_spark_accepted_as_spark_family(
        self,
        config_env: Path,
        tmp_path: Path,
    ) -> None:
        """``databricks_spark`` resolves to the spark family."""
        from ovid2.drivers.engine_compat import (
            assert_engine_type_supports_drivers,
        )

        host = tmp_path / "in"
        host.mkdir()
        _conn(
            config_env,
            "d_conn",
            {
                "type": "disc",
                "args": {"host_path": str(host)},
            },
        )
        _addr(
            config_env,
            "disc_input",
            {
                "connection": "d_conn",
                "location": {},
            },
        )
        assert (
            assert_engine_type_supports_drivers(
                "databricks_spark",
                ["disc_input"],
            )
            is None
        )

    def test_empty_address_list_is_ok(self) -> None:
        from ovid2.drivers.engine_compat import (
            assert_engine_type_supports_drivers,
        )

        assert assert_engine_type_supports_drivers("snowpark", []) is None

    def test_unresolvable_address_is_skipped(self, config_env: Path) -> None:
        """An address whose driver can't be resolved never blocks a run."""
        from ovid2.drivers.engine_compat import (
            assert_engine_type_supports_drivers,
        )

        assert assert_engine_type_supports_drivers("snowpark", ["nope"]) is None
