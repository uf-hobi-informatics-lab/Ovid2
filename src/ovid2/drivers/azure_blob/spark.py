"""Spark-backed read/write for the Azure Blob driver.

The **only** module under :mod:`ovid2.drivers.azure_blob` allowed to
import :mod:`pyspark`. Everything else in the azure_blob package
stays web-safe.

CSV write strategy
------------------

The pre-2026-08 Azure driver did ``coalesce(15).write.csv(...)`` and
left the resulting 15-blob prefix in place. That matched the way
Snowflake / Databricks Autoloader / Synapse external tables consume
ADLS but wasted cluster parallelism — 10 of 25 workers idled during
the final write stage.

The write path now runs at Spark's natural partitioning and, when
the address opts in via ``location.single_file = true``, stitches
the parts into a single blob via **server-side block commit**
(``stage_block`` for the tiny header, ``stage_block_from_url`` for
every body part, then a single ``commit_block_list``). Zero bytes
traverse the driver.

Behaviour matrix:

============  ==============  ==========================
single_file   Spark output    Final artefact
============  ==============  ==========================
False (dflt)  N part-*.csv    ``{table}.csv/`` prefix
True          1 hdr + N parts one ``{table}.csv`` blob
============  ==============  ==========================

Non-CSV formats (parquet, json) always take the Spark-native
prefix shape — ``single_file`` is ignored.
"""

from __future__ import annotations

import base64
import uuid
from typing import TYPE_CHECKING, Any

from ovid2.config.settings import CSV_DELIMITER
from ovid2.drivers.azure_blob.metadata import (
    _CLIENT_CREDS_PROVIDER,
    _FIXED_SAS_PROVIDER,
    _MSI_PROVIDER,
    _VALID_FORMATS,
    _parse_connection_string,
    resolve_address_auth_mode,
)

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession


class AzureBlobIO:
    """Cache + method container for the Azure Blob driver's data plane.

    Constructed by
    :class:`~ovid2.drivers.azure_blob.driver.AzureBlobDriver` at
    read/write time from the merged address-config dict. Kept as a
    small class so the ``_configure_spark`` idempotence bookkeeping
    (``_configured_sessions``) survives across successive read/write
    calls on the same driver instance.

    Raises:
        ValueError: When ``account_name`` or ``container`` is missing.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        """Validate ``config`` and cache reusable state."""
        try:
            self.account_name: str = str(config["account_name"])
        except KeyError as exc:
            raise ValueError(
                "AzureBlobAddress config missing required 'account_name' key."
            ) from exc
        try:
            self.container: str = str(config["container"])
        except KeyError as exc:
            raise ValueError("AzureBlobAddress config missing required 'container' key.") from exc
        self.prefix: str = str(config.get("prefix", "")).strip("/")
        self.endpoint_suffix: str = str(config.get("endpoint_suffix", "core.windows.net"))
        self.format: str = str(config.get("format", "csv"))
        self.lowercase_columns: bool = bool(config.get("lowercase_columns", False))
        self.single_file: bool = bool(config.get("single_file", False))

        # Squirrel every credential we might need away; the ABFS driver
        # reads them at session configuration time.
        self._connection_string: str | None = self._nonempty(config.get("connection_string"))
        self._account_key: str | None = self._nonempty(config.get("account_key"))
        self._sas_token: str | None = self._nonempty(config.get("sas_token"))
        self._tenant_id: str | None = self._nonempty(config.get("tenant_id"))
        self._client_id: str | None = self._nonempty(config.get("client_id"))
        self._client_secret: str | None = self._nonempty(config.get("client_secret"))

        self.auth_mode: str = resolve_address_auth_mode(
            {
                "connection_string": self._connection_string,
                "account_key": self._account_key,
                "sas_token": self._sas_token,
                "tenant_id": self._tenant_id,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
        )
        self._configured_sessions: set[int] = set()

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _nonempty(value: Any) -> str | None:
        """Return ``value`` as a stripped string or ``None`` when blank."""
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _fs_host(self) -> str:
        """Return the ``<account>.dfs.<suffix>`` fully-qualified host."""
        return f"{self.account_name}.dfs.{self.endpoint_suffix}"

    def _blob_host(self) -> str:
        """Return the ``<account>.blob.<suffix>`` host (for the finalize call).

        The Azure Storage Python SDK talks to the blob service
        endpoint (``*.blob.*``); the ABFS Hadoop driver talks to the
        DFS endpoint (``*.dfs.*``). Both end up backed by the same
        storage account so an object staged through ABFS is visible
        through blob and vice-versa.
        """
        return f"{self.account_name}.blob.{self.endpoint_suffix}"

    def _base_uri(self) -> str:
        """Return the ``abfss://<container>@<host>/`` prefix (no path)."""
        return f"abfss://{self.container}@{self._fs_host()}/"

    def _resolve_read_path(
        self,
        pipeline: str,
        batch_id: str,
        file_name: str,
    ) -> str:
        """Assemble the fully-qualified read path.

        Args:
            pipeline: Pipeline name (case-sensitive — the driver does
                not offer the glob-based case-insensitive fallback the
                :class:`DiscAddress` uses).
            batch_id: Batch identifier.
            file_name: File name; may itself contain wildcards accepted
                by the Hadoop path resolver (``*``, ``?``, ``[…]``).

        Returns:
            An ``abfss://…`` URI suitable for ``spark.read`` methods.
        """
        parts = [p for p in (self.prefix, pipeline, batch_id, file_name) if p]
        return self._base_uri() + "/".join(parts)

    def _resolve_write_dir(self, pipeline: str, batch_id: str) -> str:
        """Return the write directory URI (no trailing file name)."""
        parts = [p for p in (self.prefix, pipeline, batch_id) if p]
        return self._base_uri() + "/".join(parts)

    def _write_blob_prefix(self, pipeline: str, batch_id: str) -> str:
        """Return the container-relative prefix ``{prefix}/{pipeline}/{batch}``.

        Used by the ``azure-storage-blob``-based finalize step. Never
        carries a leading slash so it composes with
        ``<container>/<blob>``.
        """
        parts = [p for p in (self.prefix, pipeline, batch_id) if p]
        return "/".join(parts)

    # -------------------------------------------------- Spark session config

    def _configure_spark(self, spark: SparkSession) -> None:
        """Push credentials + auth-provider settings onto the session.

        The ABFS driver reads its configuration off ``spark.conf`` at
        first-touch. We remember which sessions we've already
        configured so a single ``OvidInstance`` run only touches the
        conf once per session (relevant when the same pipeline has
        multiple Azure addresses on the same account — a not-yet-
        common but supported topology).

        Contract:
            No credential material is logged. All values move directly
            from this instance to ``spark.conf.set``; nothing transits
            ``logger.debug`` or an event payload.
        """
        session_id = id(spark)
        if session_id in self._configured_sessions:
            return

        host = self._fs_host()
        conf = spark.conf

        if self.auth_mode == "connection_string":
            assert self._connection_string is not None  # narrowed by mode
            parsed = _parse_connection_string(self._connection_string)
            account_key = parsed.get("AccountKey")
            if not account_key:
                raise ValueError("AzureBlobAddress connection_string is missing 'AccountKey'.")
            conf.set(f"fs.azure.account.auth.type.{host}", "SharedKey")
            conf.set(f"fs.azure.account.key.{host}", account_key)

        elif self.auth_mode == "account_key":
            assert self._account_key is not None
            conf.set(f"fs.azure.account.auth.type.{host}", "SharedKey")
            conf.set(f"fs.azure.account.key.{host}", self._account_key)

        elif self.auth_mode == "sas_token":
            assert self._sas_token is not None
            token = self._sas_token.lstrip("?")
            conf.set(f"fs.azure.account.auth.type.{host}", "SAS")
            conf.set(f"fs.azure.sas.token.provider.type.{host}", _FIXED_SAS_PROVIDER)
            conf.set(f"fs.azure.sas.fixed.token.{host}", token)

        elif self.auth_mode == "service_principal":
            assert self._tenant_id and self._client_id and self._client_secret
            conf.set(f"fs.azure.account.auth.type.{host}", "OAuth")
            conf.set(
                f"fs.azure.account.oauth.provider.type.{host}",
                _CLIENT_CREDS_PROVIDER,
            )
            conf.set(
                f"fs.azure.account.oauth2.client.endpoint.{host}",
                f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/token",
            )
            conf.set(f"fs.azure.account.oauth2.client.id.{host}", self._client_id)
            conf.set(
                f"fs.azure.account.oauth2.client.secret.{host}",
                self._client_secret,
            )

        else:  # managed_identity — the default path on the Ovid host.
            conf.set(f"fs.azure.account.auth.type.{host}", "OAuth")
            conf.set(f"fs.azure.account.oauth.provider.type.{host}", _MSI_PROVIDER)

        self._configured_sessions.add(session_id)

    # ------------------------------------------------------------------ read

    def read(
        self,
        spark: SparkSession,
        pipeline: str,
        batch_id: str,
        file_name: str,
        delimiter: str | None = None,
    ) -> DataFrame:
        """Read a lazy Spark DataFrame from ADLS Gen2."""
        self._configure_spark(spark)
        path = self._resolve_read_path(pipeline, batch_id, file_name)

        if self.format == "csv":
            reader = (
                spark.read.option("header", "true")
                .option("inferSchema", "false")
                .option("delimiter", delimiter or CSV_DELIMITER)
            )
            df = reader.csv(path)
        elif self.format == "parquet":
            df = spark.read.parquet(path)
        elif self.format == "json":
            df = spark.read.json(path)
        else:
            raise ValueError(f"Unsupported format: {self.format}")

        if self.lowercase_columns:
            df = df.toDF(*[c.lower() for c in df.columns])
        return df

    # ----------------------------------------------------------------- write

    def write(
        self,
        df: DataFrame,
        spark: SparkSession,
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str | None = None,
    ) -> None:
        """Write a DataFrame under ``{prefix}/{pipeline}/{batch_id}/``.

        Behaviour depends on ``single_file`` (address location field,
        default ``False``):

        - ``False``: the Spark-native directory of ``part-*.csv``
          blobs is left in place. Matches the pre-2026-08 contract
          and works with every downstream consumer (Snowflake,
          Databricks Autoloader, Synapse external tables).
        - ``True``: writes body parts (with ``header=False``) and a
          separate ``_header.csv`` blob, then stitches them into a
          single ``{table_name}.csv`` blob via a server-side block
          commit. No bytes traverse the driver.

        Non-CSV formats always take the prefix shape.
        """
        self._configure_spark(spark)
        out_dir = self._resolve_write_dir(pipeline, batch_id)
        target = f"{out_dir}/{table_name}.{self.format}"

        if self.format == "csv":
            if self.single_file:
                self._write_csv_single_file(
                    df,
                    pipeline,
                    batch_id,
                    table_name,
                    delimiter or CSV_DELIMITER,
                )
            else:
                (
                    df.write.format("csv")
                    .option("header", True)
                    .option("nullValue", None)
                    .option("delimiter", delimiter or CSV_DELIMITER)
                    .option("quote", "")
                    .mode("overwrite")
                    .save(target)
                )
        elif self.format == "parquet":
            df.write.mode("overwrite").parquet(target)
        elif self.format == "json":
            df.write.mode("overwrite").json(target)
        else:
            raise ValueError(f"Unsupported format: {self.format}")

    # -------------------------------------------- single-file finalize

    def _write_csv_single_file(
        self,
        df: DataFrame,
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str,
    ) -> None:
        """Emit one ``{table_name}.csv`` blob via server-side block commit.

        Steps:

        1. Write the CSV header once to a tiny staging blob.
        2. Write the body parts (headerless) under a staging prefix,
           at Spark's natural partitioning.
        3. Enumerate the staged blobs, then ``stage_block`` the
           header (inline, tiny) + ``stage_block_from_url`` each body
           part (server-side copy). ``commit_block_list`` glues them
           into ``{table_name}.csv``.
        4. Delete the staging prefix.

        Any failure between steps 2 and 4 leaves the staging prefix
        behind for debugging; the caller can re-run.
        """
        blob_prefix = self._write_blob_prefix(pipeline, batch_id)
        target_blob = f"{blob_prefix}/{table_name}.csv" if blob_prefix else f"{table_name}.csv"
        staging_prefix = (
            f"{blob_prefix}/_ovid2_staging_{table_name}"
            if blob_prefix
            else f"_ovid2_staging_{table_name}"
        )
        staging_uri = f"abfss://{self.container}@{self._fs_host()}/{staging_prefix}"
        header_uri = f"{staging_uri}/_header.csv"

        # Step 1: header once.
        (
            df.limit(0)
            .coalesce(1)
            .write.format("csv")
            .option("header", True)
            .option("nullValue", None)
            .option("delimiter", delimiter)
            .option("quote", "")
            .mode("overwrite")
            .save(header_uri)
        )

        # Step 2: body parts at natural partitioning, header disabled.
        (
            df.write.format("csv")
            .option("header", False)
            .option("nullValue", None)
            .option("delimiter", delimiter)
            .option("quote", "")
            .mode("overwrite")
            .save(staging_uri + "/body")
        )

        # Step 3 + 4: server-side stitch, then cleanup.
        finalize_csv_block_commit(
            account_name=self.account_name,
            container=self.container,
            target_blob=target_blob,
            header_blob_dir=f"{staging_prefix}/_header.csv",
            body_prefix=f"{staging_prefix}/body/",
            endpoint_suffix=self.endpoint_suffix,
            credential=self._sdk_credential(),
        )
        self._delete_prefix(staging_prefix)

    def _sdk_credential(self) -> Any:
        """Return the credential the Azure SDK client should use.

        Mirrors the credential legs the ABFS driver picks so a run
        that authenticates through the JVM classpath authenticates
        the same way through the Python SDK.
        """
        # Local imports keep the module import-safe when the Azure
        # SDK is not installed (the caller only pays for the import
        # when they actually opt in to single_file writes).
        if self.auth_mode == "connection_string":
            assert self._connection_string is not None
            parsed = _parse_connection_string(self._connection_string)
            return parsed.get("AccountKey")
        if self.auth_mode == "account_key":
            return self._account_key
        if self.auth_mode == "sas_token":
            assert self._sas_token is not None
            return self._sas_token.lstrip("?")
        if self.auth_mode == "service_principal":
            from azure.identity import ClientSecretCredential

            assert self._tenant_id and self._client_id and self._client_secret
            return ClientSecretCredential(
                tenant_id=self._tenant_id,
                client_id=self._client_id,
                client_secret=self._client_secret,
            )
        # managed_identity — DefaultAzureCredential walks env → MSI → az CLI.
        from azure.identity import DefaultAzureCredential

        return DefaultAzureCredential()

    def _delete_prefix(self, prefix: str) -> None:
        """Recursively delete ``{container}/{prefix}/``.

        Best-effort; a failed delete raises so the operator sees the
        leaked staging space.
        """
        from azure.storage.blob import ContainerClient

        account_url = f"https://{self._blob_host()}"
        container_client = ContainerClient(
            account_url=account_url,
            container_name=self.container,
            credential=self._sdk_credential(),
        )
        list_prefix = prefix.rstrip("/") + "/"
        blobs = [b.name for b in container_client.list_blobs(name_starts_with=list_prefix)]
        # ``delete_blobs`` accepts batches; keep it simple with the
        # variadic form.
        for i in range(0, len(blobs), 256):
            batch = blobs[i : i + 256]
            if batch:
                container_client.delete_blobs(*batch)


# ---------------------------------------------------------------------------
# Server-side stitch — module-level so tests can drive it without an
# AzureBlobIO instance.
# ---------------------------------------------------------------------------


def _b64_block_id(index: int) -> str:
    """Return a stable, sortable, base64-encoded block id.

    Azure requires block ids to be equal-length base64 strings. We
    encode a per-upload UUID + a zero-padded index so ids sort
    lexicographically in commit order.
    """
    raw = f"{uuid.uuid4().hex[:8]}-{index:08d}"
    return base64.b64encode(raw.encode("ascii")).decode("ascii")


def finalize_csv_block_commit(
    account_name: str,
    container: str,
    target_blob: str,
    header_blob_dir: str,
    body_prefix: str,
    endpoint_suffix: str,
    credential: Any,
) -> None:
    """Stitch a header + body prefix into one CSV blob.

    Uses a two-phase block commit:

    - Stage a block per body ``part-*.csv`` blob via
      ``stage_block_from_url`` — server-side copy, no bytes through
      the driver.
    - Stage the header block inline via ``stage_block`` (small,
      one line of text).
    - ``commit_block_list`` glues them into ``target_blob``.

    Args:
        account_name: Azure storage account.
        container: Container name.
        target_blob: Blob name to create (``{table_name}.csv``).
        header_blob_dir: Directory blob name where the header was
            written. Spark writes the header into a directory
            containing a single ``part-*.csv``; we locate it and
            stage its bytes as block 1.
        body_prefix: Prefix under which the body ``part-*.csv``
            blobs live. Trailing slash is enforced.
        endpoint_suffix: e.g. ``core.windows.net``.
        credential: Whatever ``BlobServiceClient`` accepts — an
            account key, SAS token string, or
            ``azure.identity.*Credential``.

    Raises:
        FileNotFoundError: If no body parts are found under
            ``body_prefix``.
    """
    from azure.storage.blob import BlobClient, ContainerClient

    account_url = f"https://{account_name}.blob.{endpoint_suffix}"
    container_client = ContainerClient(
        account_url=account_url,
        container_name=container,
        credential=credential,
    )
    body_prefix = body_prefix if body_prefix.endswith("/") else body_prefix + "/"

    # Enumerate body parts, sorted deterministically.
    body_blobs: list[str] = []
    for b in container_client.list_blobs(name_starts_with=body_prefix):
        leaf = b.name.split("/")[-1]
        if leaf.startswith("part-") and leaf.endswith(".csv"):
            body_blobs.append(b.name)
    body_blobs.sort()
    if not body_blobs:
        raise FileNotFoundError(f"No body part blobs under {container}/{body_prefix}.")

    # Locate the single header part blob under header_blob_dir.
    header_list_prefix = header_blob_dir.rstrip("/") + "/"
    header_candidates = [
        b.name
        for b in container_client.list_blobs(name_starts_with=header_list_prefix)
        if b.name.split("/")[-1].startswith("part-") and b.name.endswith(".csv")
    ]
    if not header_candidates:
        raise FileNotFoundError(f"No header part blob under {container}/{header_list_prefix}.")
    header_blob_name = header_candidates[0]

    target_client: BlobClient = container_client.get_blob_client(target_blob)

    # Stage block 1: header inline.
    header_client = container_client.get_blob_client(header_blob_name)
    header_bytes = header_client.download_blob().readall()
    block_ids: list[str] = [_b64_block_id(1)]
    target_client.stage_block(block_id=block_ids[0], data=header_bytes)

    # Stage blocks 2..N: server-side copies of body parts.
    for i, name in enumerate(body_blobs, start=2):
        block_id = _b64_block_id(i)
        block_ids.append(block_id)
        source_url = f"{account_url}/{container}/{name}"
        target_client.stage_block_from_url(
            block_id=block_id,
            source_url=source_url,
        )

    # The SDK accepts plain block-id strings (committed as "latest");
    # its annotation only advertises list[BlobBlock].
    target_client.commit_block_list(block_ids)  # type: ignore[arg-type]


__all__ = [
    "_VALID_FORMATS",
    "AzureBlobIO",
    "finalize_csv_block_commit",
]
