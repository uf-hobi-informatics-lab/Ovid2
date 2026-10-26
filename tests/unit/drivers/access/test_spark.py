"""Unit tests for the pyspark-free helpers in :mod:`ovid2.drivers.access.spark`.

The JDBC read / enumeration paths need a live JVM and are exercised by
the opt-in integration test; here we lock in the pure string logic
(system-table filtering, identifier quoting) that runs without Spark.
"""

from __future__ import annotations

from ovid2.drivers.access import spark as access_spark


class TestIsSystemTable:
    """System-table / schema filtering."""

    def test_msys_prefix_is_system(self) -> None:
        assert access_spark.is_system_table("MSysObjects") is True
        assert access_spark.is_system_table("msysqueries") is True

    def test_tilde_temp_is_system(self) -> None:
        assert access_spark.is_system_table("~TMPCLP") is True

    def test_information_schema_is_system(self) -> None:
        assert access_spark.is_system_table("TABLES", "INFORMATION_SCHEMA") is True

    def test_user_table_is_not_system(self) -> None:
        assert access_spark.is_system_table("DrugNames") is False
        assert access_spark.is_system_table("RXCUI", "PUBLIC_USER") is False


class TestQuoteIdentifier:
    """Table-name quoting for the dbtable option.

    UCanAccess parses double-quoted names as string literals, so safe
    identifiers are passed bare and only awkward names are bracketed.
    """

    def test_safe_identifier_is_bare(self) -> None:
        assert access_spark._quote_identifier("alr_category") == "alr_category"

    def test_camel_case_is_bare(self) -> None:
        assert access_spark._quote_identifier("DrugNames") == "DrugNames"

    def test_with_space_is_bracketed(self) -> None:
        assert access_spark._quote_identifier("Drug Names") == "[Drug Names]"

    def test_leading_digit_is_bracketed(self) -> None:
        assert access_spark._quote_identifier("1st_table") == "[1st_table]"

    def test_embedded_bracket_stripped(self) -> None:
        assert access_spark._quote_identifier("a]b c") == "[ab c]"


class TestAllStringCustomSchema:
    """The all-STRING ``customSchema`` builder (a ``copy`` moves tables
    verbatim and needs no numeric fidelity; all-STRING also insulates the
    read from Access column-type surprises).
    """

    def test_every_column_typed_string(self) -> None:
        schema = access_spark._all_string_custom_schema(["class_id", "class_name"])
        assert schema == "`class_id` STRING, `class_name` STRING"

    def test_single_column(self) -> None:
        assert access_spark._all_string_custom_schema(["x"]) == "`x` STRING"

    def test_empty_columns(self) -> None:
        assert access_spark._all_string_custom_schema([]) == ""

    def test_backtick_is_doubled(self) -> None:
        # Backtick is Spark's identifier quote; doubling it is both valid
        # DDL and closes the only injection vector.
        assert access_spark._all_string_custom_schema(["a`b"]) == "`a``b` STRING"


class TestRegisterDialect:
    """Driver-side registration of the UCanAccess ``JdbcDialect``.

    The dialect class is compiled into the image at build time, so the
    pure-Python unit tier can only exercise the registration *plumbing*
    against a fake JVM gateway (the real bracket-quoting behaviour is
    covered by the opt-in integration test). These tests lock in that
    :func:`_register_dialect` (a) resolves the dialect via the thread
    context classloader — the system classloader cannot see ``--jars`` —
    and (b) fails loudly with rebuild guidance when the jar is absent.
    """

    def test_registers_via_context_classloader(self) -> None:
        calls: dict[str, object] = {}

        class _FakeClass:
            @staticmethod
            def forName(name: str, init: bool, loader: object) -> object:
                calls["forName"] = (name, init, loader)

                class _Cls:
                    @staticmethod
                    def newInstance() -> str:
                        return "dialect-instance"

                return _Cls

        class _FakeThread:
            @staticmethod
            def currentThread() -> object:
                class _T:
                    @staticmethod
                    def getContextClassLoader() -> str:
                        return "ctx-cl"

                return _T

        class _FakeDialects:
            @staticmethod
            def registerDialect(instance: object) -> None:
                calls["registered"] = instance

        class _FakeJvm:
            class java:
                class lang:
                    Class = _FakeClass
                    Thread = _FakeThread

            class org:
                class apache:
                    class spark:
                        class sql:
                            class jdbc:
                                JdbcDialects = _FakeDialects

        class _FakeSparkContext:
            _jvm = _FakeJvm

        class _FakeSpark:
            sparkContext = _FakeSparkContext

        access_spark._register_dialect(_FakeSpark())  # type: ignore[arg-type]

        name, init, loader = calls["forName"]  # type: ignore[misc]
        assert name == access_spark._DIALECT_CLASS
        assert init is True
        assert loader == "ctx-cl"
        assert calls["registered"] == "dialect-instance"

    def test_missing_jar_raises_with_rebuild_hint(self) -> None:
        class _FakeThread:
            @staticmethod
            def currentThread() -> object:
                class _T:
                    @staticmethod
                    def getContextClassLoader() -> str:
                        return "ctx-cl"

                return _T

        class _FakeClass:
            @staticmethod
            def forName(name: str, init: bool, loader: object) -> object:
                raise RuntimeError("ClassNotFoundException")

        class _FakeJvm:
            class java:
                class lang:
                    Class = _FakeClass
                    Thread = _FakeThread

        class _FakeSparkContext:
            _jvm = _FakeJvm

        class _FakeSpark:
            sparkContext = _FakeSparkContext

        import pytest

        with pytest.raises(RuntimeError, match=r"not on the.*classpath"):
            access_spark._register_dialect(_FakeSpark())  # type: ignore[arg-type]


class TestJdbcUrl:
    """The UCanAccess URL carries the memory-safety + mirror options."""

    def test_url_shape(self) -> None:
        url = access_spark._jdbc_url("/mnt/refdb/drugs.accdb")
        assert url.startswith("jdbc:ucanaccess:///mnt/refdb/drugs.accdb")
        assert "memory=false" in url
        assert "skipIndexes=true" in url

    def test_url_includes_writable_mirror(self) -> None:
        url = access_spark._jdbc_url("/mnt/refdb/drugs.accdb")
        assert "mirrorFolder=" in url

    def test_mirror_folder_respects_env(self, tmp_path, monkeypatch) -> None:
        target = tmp_path / "mirror"
        monkeypatch.setenv("OVID2_ACCESS_MIRROR_DIR", str(target))
        folder = access_spark._mirror_folder()
        assert folder == str(target)
        assert target.is_dir()  # created on demand

    def test_mirror_folder_prefers_image_baked_dir(self, tmp_path, monkeypatch) -> None:
        """When the ETL image's chmod-777 dir exists, default there.

        The JDBC read runs on the Spark *executor* (worker container),
        where ``/tmp`` is per-container and any driver-side
        :func:`os.makedirs` never ran. The image bakes
        ``/docker_data/spark_tmp`` chmod-777 on every container, so the
        default must resolve to it (it exists on the executor JVM) rather
        than a per-container ``/tmp`` path that UCanAccess cannot create.
        """
        monkeypatch.delenv("OVID2_ACCESS_MIRROR_DIR", raising=False)
        baked = tmp_path / "docker_data" / "spark_tmp"
        baked.mkdir(parents=True)
        monkeypatch.setattr(access_spark, "_IMAGE_BAKED_MIRROR_DIR", str(baked))
        assert access_spark._mirror_folder() == str(baked)

    def test_mirror_folder_falls_back_to_tempdir_off_image(self, tmp_path, monkeypatch) -> None:
        """Off the ETL image (tests / plain-python) fall back to tempdir.

        The baked dir does not exist outside the image, so the resolver
        must create and return a scratch subdir under the system temp
        dir instead of pointing at a non-existent path.
        """
        monkeypatch.delenv("OVID2_ACCESS_MIRROR_DIR", raising=False)
        missing = tmp_path / "does" / "not" / "exist"
        monkeypatch.setattr(access_spark, "_IMAGE_BAKED_MIRROR_DIR", str(missing))
        monkeypatch.setattr(access_spark.tempfile, "gettempdir", lambda: str(tmp_path))
        folder = access_spark._mirror_folder()
        assert folder == str(tmp_path / "ovid2_access_mirror")
        assert (tmp_path / "ovid2_access_mirror").is_dir()
