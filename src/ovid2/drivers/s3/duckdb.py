"""DuckDB data plane for the s3 driver (boto3 transfer + local DuckDB).

Credentials follow :func:`ovid2.drivers.s3.metadata.resolve_auth_mode`
exactly like the Spark ``S3IO`` path (static / session keys, or the
default boto3 chain). Objects are exchanged through the run's scratch
directory; see :mod:`ovid2.drivers._object_store_duckdb`.
"""

from __future__ import annotations

from typing import Any

from ovid2.core.engines.duckdb_stager import register_duckdb_strategy
from ovid2.drivers._object_store_duckdb import ObjectStoreClient, ObjectStoreStrategy
from ovid2.drivers.s3.metadata import resolve_auth_mode


def boto3_kwargs(config: dict[str, Any]) -> dict[str, Any]:
    """Return ``boto3.client('s3', **kwargs)`` kwargs for a merged config."""
    kwargs: dict[str, Any] = {}
    if config.get("region"):
        kwargs["region_name"] = config["region"]
    if config.get("endpoint_url"):
        kwargs["endpoint_url"] = config["endpoint_url"]
    mode = resolve_auth_mode(config)
    if mode in ("static", "session"):
        kwargs["aws_access_key_id"] = config.get("aws_access_key_id")
        kwargs["aws_secret_access_key"] = config.get("aws_secret_access_key")
        if mode == "session":
            kwargs["aws_session_token"] = config.get("aws_session_token")
    if config.get("signature_version"):
        from botocore.config import Config

        kwargs["config"] = Config(signature_version=str(config["signature_version"]))
    return kwargs


class S3Client(ObjectStoreClient):
    """boto3-backed :class:`ObjectStoreClient`."""

    def __init__(self, config: dict[str, Any]) -> None:
        import boto3

        bucket = config.get("bucket")
        if not bucket:
            raise ValueError("s3 address has no bucket (set location.bucket or bucket_default)")
        self.bucket = str(bucket)
        self._s3 = boto3.client("s3", **boto3_kwargs(config))

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", ()))
        return keys

    def download(self, key: str, dest: str) -> None:
        self._s3.download_file(self.bucket, key, dest)

    def upload(self, src: str, key: str) -> None:
        self._s3.upload_file(src, self.bucket, key)

    def delete(self, keys: list[str]) -> None:
        for i in range(0, len(keys), 1000):
            batch: list[Any] = [{"Key": k} for k in keys[i : i + 1000]]
            if batch:
                self._s3.delete_objects(
                    Bucket=self.bucket, Delete={"Objects": batch, "Quiet": True}
                )


@register_duckdb_strategy
class DuckDBS3Strategy(ObjectStoreStrategy):
    """s3 <-> DuckDB."""

    driver_type = "s3"

    def client(self, config: dict[str, Any]) -> ObjectStoreClient:
        return S3Client(config)
