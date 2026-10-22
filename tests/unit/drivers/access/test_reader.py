"""Unit tests for :mod:`ovid2.drivers.access.reader` (Spark-free path)."""

from __future__ import annotations

import pytest

from ovid2.drivers.access import reader


class TestSystemTableFilter:
    def test_msys_prefix_hidden(self) -> None:
        assert reader.is_system_table("MSysObjects") is True

    def test_tilde_temp_hidden(self) -> None:
        assert reader.is_system_table("~TMPCLP") is True

    def test_system_schema_hidden(self) -> None:
        assert reader.is_system_table("Drugs", "INFORMATION_SCHEMA") is True

    def test_user_table_kept(self) -> None:
        assert reader.is_system_table("Drugs") is False
        assert reader.is_system_table("Ndc", "PUBLIC_DATA") is False


class TestJdbcUrl:
    def test_url_shape(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("OVID2_ACCESS_MIRROR_DIR", str(tmp_path))
        url = reader.jdbc_url("/mnt/v.accdb")
        assert url.startswith("jdbc:ucanaccess:///mnt/v.accdb")
        assert "memory=false" in url
        assert "skipIndexes=true" in url
        assert f"mirrorFolder={tmp_path}" in url


class TestJarResolver:
    def test_env_override_dir(self, monkeypatch, tmp_path) -> None:
        (tmp_path / "ucanaccess.jar").write_bytes(b"x")
        (tmp_path / "hsqldb.jar").write_bytes(b"x")
        (tmp_path / "notes.txt").write_text("skip")
        monkeypatch.delenv("SPARK_HOME", raising=False)
        monkeypatch.setenv("OVID2_UCANACCESS_JARS", str(tmp_path))
        jars = reader.ucanaccess_jars()
        assert jars == sorted(
            [
                str(tmp_path / "hsqldb.jar"),
                str(tmp_path / "ucanaccess.jar"),
            ]
        )

    def test_env_override_explicit_file(self, monkeypatch, tmp_path) -> None:
        jar = tmp_path / "ucanaccess.jar"
        jar.write_bytes(b"x")
        monkeypatch.delenv("SPARK_HOME", raising=False)
        monkeypatch.setenv("OVID2_UCANACCESS_JARS", str(jar))
        assert reader.ucanaccess_jars() == [str(jar)]

    def test_spark_home_default(self, monkeypatch, tmp_path) -> None:
        jars_dir = tmp_path / "jars"
        jars_dir.mkdir()
        (jars_dir / "ucanaccess.jar").write_bytes(b"x")
        monkeypatch.delenv("OVID2_UCANACCESS_JARS", raising=False)
        monkeypatch.setenv("SPARK_HOME", str(tmp_path))
        assert reader.ucanaccess_jars() == [str(jars_dir / "ucanaccess.jar")]

    def test_no_jars_returns_empty(self, monkeypatch) -> None:
        monkeypatch.delenv("OVID2_UCANACCESS_JARS", raising=False)
        monkeypatch.delenv("SPARK_HOME", raising=False)
        assert reader.ucanaccess_jars() == []


class TestListTablesGracefulDegradation:
    def test_no_jars_raises_not_implemented(self, monkeypatch, tmp_path) -> None:
        # jaydebeapi may or may not be installed; force the jar-absent
        # branch by clearing both resolution sources. When jaydebeapi is
        # missing the import guard raises first — either way it is a
        # NotImplementedError, which the caller treats as "unsupported".
        monkeypatch.delenv("OVID2_UCANACCESS_JARS", raising=False)
        monkeypatch.delenv("SPARK_HOME", raising=False)
        f = tmp_path / "v.accdb"
        f.write_bytes(b"x")
        with pytest.raises(NotImplementedError):
            reader.list_tables_no_spark(str(f))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Unit test; run with pytest.")
