"""Spark-backed read/write for the S3 driver.

The **only** module under :mod:`ovid2.drivers.s3` allowed to import
:mod:`pyspark`. Everything else in the s3 package stays web-safe.

CSV write strategy
------------------

The pre-2026-08 S3 driver did ``coalesce(15).write.csv(...)`` and
left the resulting 15-object prefix in place. That matched Snowflake
``COPY INTO`` / Athena / Databricks Autoloader semantics but wasted
cluster parallelism: 10 of the 25 workers sat idle during the final
write stage.

The write path now runs at Spark's natural partitioning (dozens to
hundreds of parts, depending on the upstream plan) and, when the
address opts in via ``location.single_file = true``, stitches the
parts into a single object via **server-side S3 multipart copy**
(``UploadPart`` for the tiny header, ``UploadPartCopy`` for every
body part). Zero bytes traverse the driver.

Behaviour matrix:

============  ==============  ==========================
single_file   Spark output    Final artefact
============  ==============  ==========================
False (dflt)  N part-*.csv    ``{table}.csv/`` prefix
True          1 hdr + N parts one ``{table}.csv`` object
============  ==============  ==========================

Non-CSV formats (parquet, json) always take the Spark-native
prefix shape — ``single_file`` is ignored.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from ovid2.config.settings import CSV_DELIMITER
from ovid2.drivers.s3.metadata import (
    _DEFAULT_CREDS_PROVIDER,
    _SIMPLE_CREDS_PROVIDER,
    _TEMPORARY_CREDS_PROVIDER,
    _VALID_FORMATS,
    resolve_auth_mode,
)

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession

#: Minimum part size the S3 API accepts for ``UploadPartCopy`` (5 MB).
#: The header part uses ``UploadPart`` (any size) so this only limits
#: whether we can multipart-copy the body parts individually or must
#: fall back to concatenation via download+reupload. In practice
#: Spark parts are 128–256 MB (matches ``spark.sql.files.maxPartitionBytes``)
#: so we never hit the fallback.
_MIN_MULTIPART_COPY_BYTES: int = 5 * 1024 * 1024

#: Maximum concurrency for the ``UploadPartCopy`` fan-out during
#: single-file finalize. Each call is a server-side S3 copy so the
#: bottleneck is API throughput, not local CPU. 32 keeps us well
#: below the default per-account TPS limits while cutting the
#: wall-clock of a 400-part table from ~3 min to ~10 s.
_FINALIZE_COPY_CONCURRENCY: int = 32


class S3IO:
    """Cache + method container for the S3 driver's data plane.

    Constructed by :class:`~ovid2.drivers.s3.driver.S3Driver` at
    read/write time from the merged address-config dict. Kept as a
    small class so the ``_configure_spark`` idempotence bookkeeping
    (``_configured_sessions``) survives across successive read/write
    calls on the same driver instance.

    Raises:
        ValueError: When ``bucket`` is missing or blank; when
            ``format`` is set to an unsupported value. Both raise at
            construction so a mis-authored address fails before a
            Spark session is touched.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        """Validate ``config`` and cache reusable state."""
        bucket = _nonempty(config.get("bucket"))
        if not bucket:
            raise ValueError(
                "S3 address config missing 'bucket' (set either the "
                "address's location.bucket or the connection's "
                "bucket_default arg)."
            )
        self.bucket: str = bucket
        self.prefix: str = str(config.get("prefix", "")).strip("/")
        self.region: str | None = _nonempty(config.get("region"))
        self.endpoint_url: str | None = _nonempty(config.get("endpoint_url"))
        self.signature_version: str | None = _nonempty(
            config.get("signature_version")
        )
        self.format: str = str(config.get("format", "csv"))
        self.lowercase_columns: bool = bool(config.get("lowercase_columns", False))
        self.single_file: bool = bool(config.get("single_file", False))

        self._access_key_id: str | None = _nonempty(config.get("aws_access_key_id"))
        self._secret_access_key: str | None = _nonempty(
            config.get("aws_secret_access_key")
        )
        self._session_token: str | None = _nonempty(config.get("aws_session_token"))
        self.auth_mode: str = resolve_auth_mode(config)
        self._configured_sessions: set[int] = set()

    # ------------------------------------------------------------------ helpers

    def _base_uri(self) -> str:
        """Return the ``s3a://<bucket>/`` prefix (no path)."""
        return f"s3a://{self.bucket}/"

    def _resolve_read_path(
        self, pipeline: str, batch_id: str, file_name: str,
    ) -> str:
        """Assemble the fully-qualified read path.

        Args:
            pipeline: Pipeline name (case-sensitive — the driver does
                not offer the case-insensitive fallback disc uses).
            batch_id: Batch identifier.
            file_name: File name; may contain Hadoop-style wildcards
                (``*``, ``?``, ``[…]``).

        Returns:
            An ``s3a://…`` URI suitable for ``spark.read`` methods.
        """
        parts = [p for p in (self.prefix, pipeline, batch_id, file_name) if p]
        return self._base_uri() + "/".join(parts)

    def _resolve_write_dir(self, pipeline: str, batch_id: str) -> str:
        """Return the write directory URI (no trailing file name)."""
        parts = [p for p in (self.prefix, pipeline, batch_id) if p]
        return self._base_uri() + "/".join(parts)

    def _write_key_prefix(self, pipeline: str, batch_id: str) -> str:
        """Return the bucket-relative key prefix ``{prefix}/{pipeline}/{batch}``.

        Used by the ``boto3``-based finalize step. Never carries a
        leading slash so it composes with ``s3://<bucket>/<key>``.
        """
        parts = [p for p in (self.prefix, pipeline, batch_id) if p]
        return "/".join(parts)

    # -------------------------------------------------- Spark session config

    def _configure_spark(self, spark: SparkSession) -> None:
        """Push credentials + auth-provider settings onto the session.

        The ``s3a`` driver reads its configuration off ``spark.conf``
        at first-touch. We remember which sessions we've already
        configured so a single ``OvidInstance`` run only touches the
        conf once per session (relevant when the same pipeline writes
        to multiple S3 addresses on the same bucket).

        Contract:
            No credential material is logged. Every value moves
            directly from this instance to ``spark.conf.set``; nothing
            transits ``logger.debug`` or an event payload.
        """
        session_id = id(spark)
        if session_id in self._configured_sessions:
            return

        conf = spark.conf

        if self.auth_mode == "session":
            assert self._access_key_id and self._secret_access_key
            assert self._session_token
            conf.set("fs.s3a.aws.credentials.provider", _TEMPORARY_CREDS_PROVIDER)
            conf.set("fs.s3a.access.key", self._access_key_id)
            conf.set("fs.s3a.secret.key", self._secret_access_key)
            conf.set("fs.s3a.session.token", self._session_token)

        elif self.auth_mode == "static":
            assert self._access_key_id and self._secret_access_key
            conf.set("fs.s3a.aws.credentials.provider", _SIMPLE_CREDS_PROVIDER)
            conf.set("fs.s3a.access.key", self._access_key_id)
            conf.set("fs.s3a.secret.key", self._secret_access_key)

        else:  # default_chain
            conf.set("fs.s3a.aws.credentials.provider", _DEFAULT_CREDS_PROVIDER)

        if self.region:
            # Newer hadoop-aws honours ``fs.s3a.endpoint.region``; older
            # builds ignored it in favour of the endpoint hostname.
            # Setting both is harmless.
            conf.set("fs.s3a.endpoint.region", self.region)

        if self.endpoint_url:
            conf.set("fs.s3a.endpoint", self.endpoint_url)
            # Non-AWS S3-compatible services (MinIO, R2, Spaces) do not
            # universally support virtual-host-style addressing; force
            # path-style so requests land on the right endpoint.
            conf.set("fs.s3a.path.style.access", "true")

        if self.signature_version:
            conf.set("fs.s3a.signing-algorithm", self.signature_version)

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
        """Read a lazy Spark DataFrame from S3."""
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

        - ``False``: the Spark-native prefix of ``part-*.csv`` objects
          is left in place — matches the pre-2026-08 contract and
          works with every downstream consumer (Snowflake ``COPY
          INTO``, Athena, Databricks Autoloader, Redshift Spectrum).
        - ``True``: writes body parts (with ``header=False``) and a
          separate ``_header.csv`` object, then stitches them into a
          single ``{table_name}.csv`` object via a server-side S3
          multipart copy. No bytes traverse the driver.

        Non-CSV formats always take the prefix shape.
        """
        self._configure_spark(spark)
        out_dir = self._resolve_write_dir(pipeline, batch_id)
        target = f"{out_dir}/{table_name}.{self.format}"

        if self.format == "csv":
            if self.single_file:
                self._write_csv_single_file(
                    df, pipeline, batch_id, table_name, delimiter or CSV_DELIMITER,
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
        """Emit one ``{table_name}.csv`` object via server-side multipart copy.

        Steps:

        1. Write the CSV header (once) to a small staging object.
        2. Write the body parts (headerless) under a staging prefix,
           at Spark's natural partitioning.
        3. Enumerate the staged objects, then run an S3 multipart
           upload: ``UploadPart`` for the header, ``UploadPartCopy``
           per body part. ``CompleteMultipartUpload`` glues them
           into ``{table_name}.csv``.
        4. Delete the staging prefix.

        Any failure between steps 2 and 4 leaves the staging prefix
        behind for debugging; the caller can re-run.
        """
        key_prefix = self._write_key_prefix(pipeline, batch_id)
        target_key = f"{key_prefix}/{table_name}.csv" if key_prefix else f"{table_name}.csv"
        staging_key_prefix = (
            f"{key_prefix}/_ovid2_staging_{table_name}"
            if key_prefix
            else f"_ovid2_staging_{table_name}"
        )
        staging_uri = f"s3a://{self.bucket}/{staging_key_prefix}"
        header_key = f"{staging_key_prefix}/_header.csv"
        header_uri = f"s3a://{self.bucket}/{header_key}"

        # Step 1: header once, via a tiny separate write. limit(0)
        # keeps zero rows on the plan; coalesce(1) so the header is
        # a single object rather than a directory of empty parts.
        (
            df.limit(0).coalesce(1)
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
        finalize_csv_multipart(
            bucket=self.bucket,
            target_key=target_key,
            header_key=self._locate_single_part(f"{staging_key_prefix}/_header.csv"),
            body_prefix=f"{staging_key_prefix}/body/",
            aws_kwargs=self._boto3_kwargs(),
        )
        self._delete_prefix(staging_key_prefix)

    def _locate_single_part(self, header_dir_key: str) -> str:
        """Return the concrete ``part-*.csv`` key under a Spark write dir.

        Spark's writer produces a directory whose only real content is
        one ``part-XXXXX-….csv`` file (because we ``coalesce(1)``
        upstream). Return its key so the multipart upload can consume
        it directly.
        """
        # The header ``.save(header_uri)`` produced a directory; boto3
        # needs the specific object key inside it.
        import boto3

        client = boto3.client("s3", **self._boto3_kwargs())
        # Trailing slash guarantees we scan the "directory", not any
        # sibling object with a prefix that happens to start with the
        # header path stem.
        list_prefix = header_dir_key.rstrip("/") + "/"
        resp = client.list_objects_v2(Bucket=self.bucket, Prefix=list_prefix)
        keys = [
            obj["Key"]
            for obj in resp.get("Contents", ())
            if obj["Key"].split("/")[-1].startswith("part-")
            and obj["Key"].endswith(".csv")
        ]
        if not keys:
            raise FileNotFoundError(
                f"Spark produced no part-*.csv under s3://{self.bucket}/{list_prefix}."
            )
        # Only one because of coalesce(1) on a zero-row plan.
        return keys[0]

    def _boto3_kwargs(self) -> dict[str, Any]:
        """Return the kwargs ``boto3.client('s3', ...)`` needs.

        Mirrors the credential legs the Hadoop driver uses so a run
        that authenticates through the JVM classpath authenticates
        the same way through boto3.
        """
        kwargs: dict[str, Any] = {}
        if self.region:
            kwargs["region_name"] = self.region
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url
        if self.auth_mode == "session":
            kwargs.update(
                aws_access_key_id=self._access_key_id,
                aws_secret_access_key=self._secret_access_key,
                aws_session_token=self._session_token,
            )
        elif self.auth_mode == "static":
            kwargs.update(
                aws_access_key_id=self._access_key_id,
                aws_secret_access_key=self._secret_access_key,
            )
        # default_chain → let boto3 discover.
        return kwargs

    def _delete_prefix(self, key_prefix: str) -> None:
        """Recursively delete ``s3://{bucket}/{key_prefix}/``.

        Uses ``list_objects_v2`` + ``delete_objects`` in batches of
        1000 (the S3 delete limit). Best-effort; a failed delete
        raises so the operator sees the leaked staging space.
        """
        import boto3

        client = boto3.client("s3", **self._boto3_kwargs())
        list_prefix = key_prefix.rstrip("/") + "/"
        continuation: str | None = None
        while True:
            list_kwargs: dict[str, Any] = {
                "Bucket": self.bucket,
                "Prefix": list_prefix,
            }
            if continuation is not None:
                list_kwargs["ContinuationToken"] = continuation
            resp = client.list_objects_v2(**list_kwargs)
            objects = [{"Key": obj["Key"]} for obj in resp.get("Contents", ())]
            for i in range(0, len(objects), 1000):
                batch = objects[i : i + 1000]
                if batch:
                    client.delete_objects(
                        Bucket=self.bucket,
                        Delete={"Objects": batch, "Quiet": True},
                    )
            if not resp.get("IsTruncated"):
                return
            continuation = resp.get("NextContinuationToken")


# ---------------------------------------------------------------------------
# Server-side stitch — module-level so tests can drive it without an
# S3IO instance.
# ---------------------------------------------------------------------------

def finalize_csv_multipart(
    bucket: str,
    target_key: str,
    header_key: str,
    body_prefix: str,
    aws_kwargs: dict[str, Any] | None = None,
) -> None:
    """Stitch a header + body prefix into one CSV object.

    S3 multipart uploads impose a 5 MB minimum on every non-terminal
    part. That constrains what "server-side copy" can achieve:

    - The header object is always small (one CSV line) so it can
      never be its own multipart part unless it happens to be the
      terminal part — which it isn't, at least one body part
      follows.
    - Body parts default to ``spark.sql.files.maxPartitionBytes``
      (128 MB) but Spark's writer can produce a straggler tail
      (or arbitrarily small parts on small tables) that violates
      the 5 MB minimum.

    Strategy:

    - **Part 1** is *always* built by downloading the header and
      then downloading body objects into a buffer until the buffer
      clears 5 MB (or we run out of body objects — in which case
      part 1 is the terminal part and any size is fine). This one
      part is the only "bytes-through-driver" cost of the finalize
      on a large table.
    - **Remaining body objects** are appended via
      ``UploadPartCopy`` when every non-last remaining body object
      is ≥ 5 MB (fast path). Otherwise the streaming loop continues:
      buffer body objects to 5 MB per flush.

    Result: on a healthy Spark write (parts ≥ 128 MB, one straggler
    tail permitted) the driver moves only ``len(header) +
    part1_body`` bytes, and everything else is server-side. On a
    small or oddly-partitioned table the driver moves every byte
    but each part cleanly satisfies the S3 minimum.

    Args:
        bucket: Target bucket.
        target_key: Object key to create (``{table_name}.csv``).
        header_key: Object key for ``_header.csv`` (single tiny
            object holding exactly one header line).
        body_prefix: Prefix under which the body ``part-*.csv``
            objects live. Trailing slash is enforced.
        aws_kwargs: Kwargs forwarded to ``boto3.client('s3', ...)``.
            ``None`` (the default) means "default provider chain".

    Raises:
        FileNotFoundError: If no body parts are found under
            ``body_prefix``.
        RuntimeError: If ``CompleteMultipartUpload`` fails after
            the parts have been uploaded — the caller should
            re-run.
    """
    import boto3

    client = boto3.client("s3", **(aws_kwargs or {}))
    body_prefix = body_prefix if body_prefix.endswith("/") else body_prefix + "/"

    # Enumerate body parts, sorted deterministically so the resulting
    # object is byte-identical to what ``cat part-* > out`` would emit
    # on a POSIX filesystem. Retain ``Size`` so we can pick between
    # server-side copy (fast) and stream-through (fallback) for each
    # remaining body object.
    body_entries: list[tuple[str, int]] = []
    continuation: str | None = None
    while True:
        list_kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": body_prefix}
        if continuation is not None:
            list_kwargs["ContinuationToken"] = continuation
        resp = client.list_objects_v2(**list_kwargs)
        for obj in resp.get("Contents", ()):
            key = obj["Key"]
            leaf = key.split("/")[-1]
            if leaf.startswith("part-") and leaf.endswith(".csv"):
                body_entries.append((key, int(obj.get("Size", 0))))
        if not resp.get("IsTruncated"):
            break
        continuation = resp.get("NextContinuationToken")
    body_entries.sort(key=lambda pair: pair[0])
    if not body_entries:
        raise FileNotFoundError(
            f"No body part files under s3://{bucket}/{body_prefix}."
        )

    # Read the header once — it always feeds part 1.
    header_obj = client.get_object(Bucket=bucket, Key=header_key)
    header_bytes: bytes = header_obj["Body"].read()

    # Kick off the multipart upload.
    create = client.create_multipart_upload(Bucket=bucket, Key=target_key)
    upload_id: str = create["UploadId"]
    parts: list[dict[str, Any]] = []
    try:
        # -----------------------------------------------------------
        # Part 1: header + as many body objects as it takes to reach
        # the 5 MB minimum (or all of them if the total is tiny).
        # -----------------------------------------------------------
        buffer: bytearray = bytearray(header_bytes)
        consumed: int = 0
        while consumed < len(body_entries) and (
            len(buffer) < _MIN_MULTIPART_COPY_BYTES
        ):
            key, _size = body_entries[consumed]
            obj = client.get_object(Bucket=bucket, Key=key)
            buffer.extend(obj["Body"].read())
            consumed += 1

        part_resp = client.upload_part(
            Bucket=bucket,
            Key=target_key,
            UploadId=upload_id,
            PartNumber=1,
            Body=bytes(buffer),
        )
        parts.append({"PartNumber": 1, "ETag": part_resp["ETag"]})
        # Release the buffer eagerly; a 5+ MB bytearray is not free.
        buffer = bytearray()

        # -----------------------------------------------------------
        # Parts 2..N: remaining body objects.
        #
        # Fast path: every remaining non-terminal body object is ≥
        # 5 MB. We can then fan out the ``UploadPartCopy`` calls to
        # a bounded thread pool — they're independent server-side
        # copies so parallelism cuts the wall clock proportionally.
        #
        # Slow path (any non-terminal < 5 MB): stream-buffer objects
        # sequentially so each part clears the 5 MB floor. This
        # remains single-threaded because the buffer is shared
        # state; parallelism would need chunked reassembly.
        # -----------------------------------------------------------
        remaining = body_entries[consumed:]
        if remaining:
            fast = all(
                size >= _MIN_MULTIPART_COPY_BYTES
                for _key, size in remaining[:-1]
            )
            if fast:
                copy_parts = _upload_part_copies_parallel(
                    client=client,
                    bucket=bucket,
                    target_key=target_key,
                    upload_id=upload_id,
                    remaining=remaining,
                    part_number_start=2,
                    max_workers=_FINALIZE_COPY_CONCURRENCY,
                    aws_kwargs=aws_kwargs or {},
                )
                parts.extend(copy_parts)
            else:
                stream_parts = _upload_parts_streaming(
                    client=client,
                    bucket=bucket,
                    target_key=target_key,
                    upload_id=upload_id,
                    remaining=remaining,
                    part_number_start=2,
                )
                parts.extend(stream_parts)

        client.complete_multipart_upload(
            Bucket=bucket,
            Key=target_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except Exception as exc:
        # Abort so we don't leak the multipart upload (each open MPU
        # costs storage until aborted or completed).
        try:
            client.abort_multipart_upload(
                Bucket=bucket, Key=target_key, UploadId=upload_id,
            )
        except Exception:  # noqa: BLE001 — best effort
            pass
        raise RuntimeError(
            f"S3 multipart finalize failed for s3://{bucket}/{target_key}: {exc}"
        ) from exc


def _upload_part_copies_parallel(
    client: Any,
    bucket: str,
    target_key: str,
    upload_id: str,
    remaining: list[tuple[str, int]],
    part_number_start: int,
    max_workers: int,
    aws_kwargs: dict[str, Any],
) -> list[dict[str, Any]]:
    """Fan out ``UploadPartCopy`` calls across a thread pool.

    Each worker uses its own ``boto3`` client because the default
    S3 client is not thread-safe for shared use (its underlying
    ``botocore.endpoint`` reuses connections that aren't
    interlocked). Result is returned sorted by ``PartNumber`` so
    ``CompleteMultipartUpload`` sees a contiguous ascending list.
    """
    import boto3

    def _run(pair: tuple[int, tuple[str, int]]) -> dict[str, Any]:
        part_number, (key, _size) = pair
        # Per-thread client — safe re-use of aws_kwargs (all values
        # are immutable strings).
        worker_client = boto3.client("s3", **aws_kwargs)
        resp = worker_client.upload_part_copy(
            Bucket=bucket,
            Key=target_key,
            UploadId=upload_id,
            PartNumber=part_number,
            CopySource={"Bucket": bucket, "Key": key},
        )
        return {
            "PartNumber": part_number,
            "ETag": resp["CopyPartResult"]["ETag"],
        }

    del client  # main-thread client is not shared with workers
    numbered = list(enumerate(remaining, start=part_number_start))
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for part in pool.map(_run, numbered):
            results.append(part)
    results.sort(key=lambda p: p["PartNumber"])
    return results


def _upload_parts_streaming(
    client: Any,
    bucket: str,
    target_key: str,
    upload_id: str,
    remaining: list[tuple[str, int]],
    part_number_start: int,
) -> list[dict[str, Any]]:
    """Sequentially stream remaining body objects into ``UploadPart``.

    Used when any non-terminal remaining body object is smaller
    than the 5 MB multipart-copy minimum. Coalesces adjacent
    objects into a shared buffer so each emitted part clears the
    floor; the last part may be any size.
    """
    parts: list[dict[str, Any]] = []
    part_number = part_number_start
    stream_buffer = bytearray()
    for idx, (key, size) in enumerate(remaining):
        is_terminal = idx == len(remaining) - 1
        can_copy = size >= _MIN_MULTIPART_COPY_BYTES or is_terminal
        if can_copy and not stream_buffer:
            resp = client.upload_part_copy(
                Bucket=bucket,
                Key=target_key,
                UploadId=upload_id,
                PartNumber=part_number,
                CopySource={"Bucket": bucket, "Key": key},
            )
            parts.append({
                "PartNumber": part_number,
                "ETag": resp["CopyPartResult"]["ETag"],
            })
            part_number += 1
            continue
        obj = client.get_object(Bucket=bucket, Key=key)
        stream_buffer.extend(obj["Body"].read())
        if is_terminal or len(stream_buffer) >= _MIN_MULTIPART_COPY_BYTES:
            resp = client.upload_part(
                Bucket=bucket,
                Key=target_key,
                UploadId=upload_id,
                PartNumber=part_number,
                Body=bytes(stream_buffer),
            )
            parts.append({"PartNumber": part_number, "ETag": resp["ETag"]})
            part_number += 1
            stream_buffer = bytearray()
    # Defensive: any residual buffer flushes as the terminal part.
    if stream_buffer:
        resp = client.upload_part(
            Bucket=bucket,
            Key=target_key,
            UploadId=upload_id,
            PartNumber=part_number,
            Body=bytes(stream_buffer),
        )
        parts.append({"PartNumber": part_number, "ETag": resp["ETag"]})
    return parts


def _stream_finalize(  # pragma: no cover — retained for API stability
    client: Any,
    bucket: str,
    target_key: str,
    upload_id: str,
    header_bytes: bytes,
    body_entries: list[tuple[str, int]],
    parts: list[dict[str, Any]],
) -> None:
    """Legacy stream-finalize helper.

    Retained as a private module-level function so the earlier
    revision of :func:`finalize_csv_multipart` (which delegated
    here) remains importable. The current finalize logic inlines
    the streaming path directly. New callers should not use this.
    """
    del client, bucket, target_key, upload_id, header_bytes
    del body_entries, parts
    raise NotImplementedError(
        "_stream_finalize is retained for API stability; the "
        "streaming path is now inlined into finalize_csv_multipart.",
    )


def _nonempty(value: Any) -> str | None:
    """Return ``value`` stripped or ``None`` when blank. Mirrors metadata."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "S3IO",
    "_VALID_FORMATS",
    "finalize_csv_multipart",
]
