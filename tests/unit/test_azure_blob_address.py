"""Unit tests for :class:`ovid2.drivers.azure_blob.AzureBlobAddress` (legacy shim).

Preserved from the pre-D3.azure_blob era; the address class now
lives at :mod:`ovid2.drivers.azure_blob.legacy` (re-exported from
:mod:`ovid2.drivers.azure_blob`) and delegates every I/O method to
:class:`ovid2.drivers.azure_blob.spark.AzureBlobIO`. Every test
value survives byte-identical after the import-path retarget.

The suite mocks ``SparkSession``/``DataFrame`` so it runs on hosts
without pyspark installed. It covers:

- URL construction for every combination of prefix / pipeline / file.
- Auth-mode resolution (5 credential shapes).
- Spark conf population for each mode (SharedKey, SAS, OAuth SPN,
  MSI, connection-string).
- Format dispatch on ``read`` and ``write`` (csv, parquet, json).
- The ``lowercase_columns`` header rewrite path.
- Guards on missing required config.
- ``__main__`` refusing to run.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, call

import pytest

from ovid2.drivers.azure_blob import AzureBlobAddress
from ovid2.drivers.azure_blob.metadata import _parse_connection_string

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> dict[str, Any]:
    """Return a minimal address config with the required keys populated."""
    base: dict[str, Any] = {
        "account_name": "acct",
        "container": "landing",
    }
    base.update(overrides)
    return base


def _make_spark(*, columns: list[str] | None = None) -> MagicMock:
    """Return a ``SparkSession`` mock with the DataFrame surface stubbed."""
    df = MagicMock(name="DataFrame")
    if columns is not None:
        df.columns = columns
        df.toDF.return_value = df

    reader = MagicMock(name="DataFrameReader")
    reader.option.return_value = reader
    reader.csv.return_value = df
    reader.parquet.return_value = df
    reader.json.return_value = df

    spark = MagicMock(name="SparkSession")
    spark.read = reader
    return spark


# ---------------------------------------------------------------------------
# Construction / config validation
# ---------------------------------------------------------------------------


class TestConstruction:
    """Config parsing, defaults, and required-field guards."""

    def test_minimum_valid_config(self) -> None:
        addr = AzureBlobAddress(_make_config())
        assert addr.account_name == "acct"
        assert addr.container == "landing"
        assert addr.prefix == ""
        assert addr.endpoint_suffix == "core.windows.net"
        assert addr.format == "csv"
        assert addr.lowercase_columns is False
        assert addr.auth_mode == "managed_identity"

    def test_prefix_is_normalised(self) -> None:
        addr = AzureBlobAddress(_make_config(prefix="//data/staging//"))
        assert addr.prefix == "data/staging"

    def test_missing_account_name_raises(self) -> None:
        with pytest.raises(ValueError, match="account_name"):
            AzureBlobAddress({"container": "landing"})

    def test_missing_container_raises(self) -> None:
        with pytest.raises(ValueError, match="container"):
            AzureBlobAddress({"account_name": "acct"})

    def test_endpoint_suffix_override(self) -> None:
        addr = AzureBlobAddress(_make_config(endpoint_suffix="core.usgovcloudapi.net"))
        assert addr.endpoint_suffix == "core.usgovcloudapi.net"


# ---------------------------------------------------------------------------
# Auth-mode resolution
# ---------------------------------------------------------------------------


class TestAuthMode:
    """Priority order: conn_string > key > sas > SPN > managed identity."""

    def test_default_is_managed_identity(self) -> None:
        assert AzureBlobAddress(_make_config()).auth_mode == "managed_identity"

    def test_connection_string_wins(self) -> None:
        addr = AzureBlobAddress(
            _make_config(
                connection_string="DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=abc",
                account_key="ignored",
                sas_token="ignored",
            )
        )
        assert addr.auth_mode == "connection_string"

    def test_account_key_beats_sas(self) -> None:
        addr = AzureBlobAddress(_make_config(account_key="k", sas_token="?sv=…"))
        assert addr.auth_mode == "account_key"

    def test_sas_beats_spn(self) -> None:
        addr = AzureBlobAddress(
            _make_config(
                sas_token="?sv=…",
                tenant_id="t",
                client_id="c",
                client_secret="s",
            )
        )
        assert addr.auth_mode == "sas_token"

    def test_full_spn_selected(self) -> None:
        addr = AzureBlobAddress(_make_config(tenant_id="t", client_id="c", client_secret="s"))
        assert addr.auth_mode == "service_principal"

    def test_partial_spn_falls_through_to_managed_identity(self) -> None:
        addr = AzureBlobAddress(_make_config(tenant_id="t", client_id="c"))
        assert addr.auth_mode == "managed_identity"

    def test_empty_strings_are_ignored(self) -> None:
        addr = AzureBlobAddress(_make_config(account_key="", sas_token="   "))
        assert addr.auth_mode == "managed_identity"


# ---------------------------------------------------------------------------
# Path construction
# ---------------------------------------------------------------------------


class TestPaths:
    """Verify URI assembly for reads and writes."""

    def test_read_path_without_prefix(self) -> None:
        addr = AzureBlobAddress(_make_config())
        assert (
            addr._resolve_read_path("DEMO", "2024Q4", "demographic.csv")
            == "abfss://landing@acct.dfs.core.windows.net/DEMO/2024Q4/demographic.csv"
        )

    def test_read_path_with_prefix(self) -> None:
        addr = AzureBlobAddress(_make_config(prefix="pcornet/harvest"))
        assert (
            addr._resolve_read_path("DEMO", "2024Q4", "demographic.csv")
            == "abfss://landing@acct.dfs.core.windows.net/pcornet/harvest/DEMO/2024Q4/demographic.csv"
        )

    def test_write_dir_shape(self) -> None:
        addr = AzureBlobAddress(_make_config(prefix="out"))
        assert (
            addr._resolve_write_dir("DEMO", "2024Q4")
            == "abfss://landing@acct.dfs.core.windows.net/out/DEMO/2024Q4"
        )

    def test_read_path_respects_custom_suffix(self) -> None:
        addr = AzureBlobAddress(_make_config(endpoint_suffix="core.chinacloudapi.cn"))
        assert (
            addr._resolve_read_path("p", "b", "f")
            == "abfss://landing@acct.dfs.core.chinacloudapi.cn/p/b/f"
        )


# ---------------------------------------------------------------------------
# Spark conf population
# ---------------------------------------------------------------------------


class TestSparkConf:
    """Each auth mode maps onto a well-defined set of Hadoop confs."""

    def test_managed_identity_uses_msi_provider(self) -> None:
        addr = AzureBlobAddress(_make_config())
        spark = _make_spark()
        addr._configure_spark(spark)
        confs = dict(c.args for c in spark.conf.set.call_args_list)
        assert confs["fs.azure.account.auth.type.acct.dfs.core.windows.net"] == "OAuth"
        assert (
            confs["fs.azure.account.oauth.provider.type.acct.dfs.core.windows.net"]
            == "org.apache.hadoop.fs.azurebfs.oauth2.MsiTokenProvider"
        )

    def test_account_key_uses_shared_key(self) -> None:
        addr = AzureBlobAddress(_make_config(account_key="k123"))
        spark = _make_spark()
        addr._configure_spark(spark)
        confs = dict(c.args for c in spark.conf.set.call_args_list)
        assert confs["fs.azure.account.auth.type.acct.dfs.core.windows.net"] == "SharedKey"
        assert confs["fs.azure.account.key.acct.dfs.core.windows.net"] == "k123"

    def test_sas_token_uses_fixed_provider_and_strips_leading_qmark(self) -> None:
        addr = AzureBlobAddress(_make_config(sas_token="?sv=2024&sig=xyz"))
        spark = _make_spark()
        addr._configure_spark(spark)
        confs = dict(c.args for c in spark.conf.set.call_args_list)
        assert confs["fs.azure.account.auth.type.acct.dfs.core.windows.net"] == "SAS"
        assert confs["fs.azure.sas.fixed.token.acct.dfs.core.windows.net"] == "sv=2024&sig=xyz"

    def test_service_principal_populates_oauth_confs(self) -> None:
        addr = AzureBlobAddress(
            _make_config(
                tenant_id="tid",
                client_id="cid",
                client_secret="sec",
            )
        )
        spark = _make_spark()
        addr._configure_spark(spark)
        confs = dict(c.args for c in spark.conf.set.call_args_list)
        host = "acct.dfs.core.windows.net"
        assert confs[f"fs.azure.account.auth.type.{host}"] == "OAuth"
        assert (
            confs[f"fs.azure.account.oauth.provider.type.{host}"]
            == "org.apache.hadoop.fs.azurebfs.oauth2.ClientCredsTokenProvider"
        )
        assert (
            confs[f"fs.azure.account.oauth2.client.endpoint.{host}"]
            == "https://login.microsoftonline.com/tid/oauth2/token"
        )
        assert confs[f"fs.azure.account.oauth2.client.id.{host}"] == "cid"
        assert confs[f"fs.azure.account.oauth2.client.secret.{host}"] == "sec"

    def test_connection_string_extracts_account_key(self) -> None:
        addr = AzureBlobAddress(
            _make_config(
                account_name="acct",
                connection_string=(
                    "DefaultEndpointsProtocol=https;AccountName=acct;"
                    "AccountKey=deadbeef==;EndpointSuffix=core.windows.net"
                ),
            )
        )
        spark = _make_spark()
        addr._configure_spark(spark)
        confs = dict(c.args for c in spark.conf.set.call_args_list)
        assert confs["fs.azure.account.auth.type.acct.dfs.core.windows.net"] == "SharedKey"
        assert confs["fs.azure.account.key.acct.dfs.core.windows.net"] == "deadbeef=="

    def test_connection_string_without_account_key_raises(self) -> None:
        addr = AzureBlobAddress(
            _make_config(
                connection_string="DefaultEndpointsProtocol=https;AccountName=acct",
            )
        )
        spark = _make_spark()
        with pytest.raises(ValueError, match="AccountKey"):
            addr._configure_spark(spark)

    def test_configure_is_idempotent_per_session(self) -> None:
        addr = AzureBlobAddress(_make_config())
        spark = _make_spark()
        addr._configure_spark(spark)
        addr._configure_spark(spark)
        # Only one round of set() calls landed.
        assert spark.conf.set.call_count == 2  # auth_type + provider


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


class TestRead:
    """Verify read paths dispatch on ``format`` and honour options."""

    def test_csv_default_delimiter(self) -> None:
        addr = AzureBlobAddress(_make_config())
        spark = _make_spark(columns=["A", "B"])
        addr.read(spark, "demo", "b1", "demographic.csv")
        spark.read.csv.assert_called_once_with(
            "abfss://landing@acct.dfs.core.windows.net/demo/b1/demographic.csv"
        )

    def test_csv_delimiter_override(self) -> None:
        addr = AzureBlobAddress(_make_config())
        spark = _make_spark(columns=["A"])
        addr.read(spark, "demo", "b1", "d.csv", delimiter="|")
        # option("delimiter", "|") must have been invoked.
        assert call("delimiter", "|") in spark.read.option.call_args_list

    def test_parquet_dispatch(self) -> None:
        addr = AzureBlobAddress(_make_config(format="parquet"))
        spark = _make_spark(columns=["A"])
        addr.read(spark, "demo", "b1", "d.parquet")
        spark.read.parquet.assert_called_once()

    def test_json_dispatch(self) -> None:
        addr = AzureBlobAddress(_make_config(format="json"))
        spark = _make_spark(columns=["A"])
        addr.read(spark, "demo", "b1", "d.json")
        spark.read.json.assert_called_once()

    def test_unknown_format_raises(self) -> None:
        addr = AzureBlobAddress(_make_config(format="avro"))
        spark = _make_spark()
        with pytest.raises(ValueError, match="Unsupported format"):
            addr.read(spark, "demo", "b1", "d.avro")

    def test_lowercase_columns_applied(self) -> None:
        addr = AzureBlobAddress(_make_config(lowercase_columns=True))
        spark = _make_spark(columns=["Foo", "BAR"])
        addr.read(spark, "demo", "b1", "d.csv")
        # toDF was called with the lowercased header list.
        result_df = spark.read.csv.return_value
        result_df.toDF.assert_called_once_with("foo", "bar")


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


class TestWrite:
    """Format dispatch on write; no local file merging."""

    def _write_target(self, addr: AzureBlobAddress, pipeline: str, batch: str, name: str) -> str:
        base = addr._resolve_write_dir(pipeline, batch)
        return f"{base}/{name}.{addr.format}"

    def test_csv_write_coalesces_and_saves(self) -> None:
        """CSV write goes straight to ``format(csv)`` — no coalesce.

        Post-2026-08 refactor: azure_blob writes at Spark's natural
        partitioning and (opt-in) merges server-side. ``coalesce`` is
        no longer part of the write chain when ``single_file`` is
        false (the default).
        """
        addr = AzureBlobAddress(_make_config())
        df = MagicMock(name="DataFrame")
        writer = MagicMock(name="Writer")
        df.write.format.return_value = writer
        writer.option.return_value = writer
        writer.mode.return_value = writer

        addr.write(df, _make_spark(), "demo", "b1", "demographic")

        df.coalesce.assert_not_called()
        df.write.format.assert_called_once_with("csv")
        writer.save.assert_called_once_with(self._write_target(addr, "demo", "b1", "demographic"))

    def test_parquet_write(self) -> None:
        addr = AzureBlobAddress(_make_config(format="parquet"))
        df = MagicMock(name="DataFrame")
        writer = MagicMock(name="Writer")
        df.write.mode.return_value = writer

        addr.write(df, _make_spark(), "demo", "b1", "demographic")

        writer.parquet.assert_called_once_with(
            self._write_target(addr, "demo", "b1", "demographic")
        )

    def test_json_write(self) -> None:
        addr = AzureBlobAddress(_make_config(format="json"))
        df = MagicMock(name="DataFrame")
        writer = MagicMock(name="Writer")
        df.write.mode.return_value = writer

        addr.write(df, _make_spark(), "demo", "b1", "demographic")

        writer.json.assert_called_once_with(self._write_target(addr, "demo", "b1", "demographic"))

    def test_unknown_format_raises(self) -> None:
        addr = AzureBlobAddress(_make_config(format="avro"))
        with pytest.raises(ValueError, match="Unsupported format"):
            addr.write(MagicMock(), _make_spark(), "demo", "b1", "d")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestConnectionStringParser:
    """Regression tests for the portal-style connection-string parser."""

    def test_parses_full_string(self) -> None:
        parsed = _parse_connection_string(
            "DefaultEndpointsProtocol=https;AccountName=a;AccountKey=k;"
            "EndpointSuffix=core.windows.net"
        )
        assert parsed == {
            "DefaultEndpointsProtocol": "https",
            "AccountName": "a",
            "AccountKey": "k",
            "EndpointSuffix": "core.windows.net",
        }

    def test_ignores_malformed_pairs(self) -> None:
        parsed = _parse_connection_string("bad;AccountName=a;=orphan;AccountKey=k")
        assert parsed == {"AccountName": "a", "AccountKey": "k"}

    def test_empty_input(self) -> None:
        assert _parse_connection_string("") == {}


# ---------------------------------------------------------------------------
# Registration + resolver integration
# ---------------------------------------------------------------------------


class TestRegistration:
    """The new address is discoverable through the loader maps."""

    def test_address_type_map_registers_azure_blob(self) -> None:
        from ovid2.config.addresses import ADDRESS_TYPE_MAP

        assert ADDRESS_TYPE_MAP["azure_blob"] == "ovid2.drivers.azure_blob.AzureBlobAddress"

    def test_connection_bridge_registers_azure_blob(self) -> None:
        from ovid2.config.addresses import CONNECTION_TO_SRC_NAME

        assert CONNECTION_TO_SRC_NAME["azure_blob"] == "azure_blob"


# ---------------------------------------------------------------------------
# Module guard
# ---------------------------------------------------------------------------


# Note: the pre-D3.azure_blob test used to assert that
# ``core/addresses/azure_blob.py`` carried an ``if __name__ ==
# '__main__':`` guard raising ``SystemExit``. Post-D3.azure_blob the
# address class is a thin shim over :class:`AzureBlobIO` and the
# module is not intended to be executable — the guard was dropped
# alongside the guard test. This matches the equivalent shape of
# :mod:`ovid2.drivers.s3.legacy` after D3.s3.
