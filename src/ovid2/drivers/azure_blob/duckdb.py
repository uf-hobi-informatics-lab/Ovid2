"""DuckDB data plane for the azure_blob driver (Azure SDK transfer + local DuckDB).

Credential precedence matches the Spark / Snowpark paths
(:func:`ovid2.drivers.azure_blob.metadata.resolve_address_auth_mode`):
connection string > account key > SAS token > service principal >
managed identity (``DefaultAzureCredential``).
"""

from __future__ import annotations

from typing import Any

from ovid2.core.engines.duckdb_stager import register_duckdb_strategy
from ovid2.drivers._object_store_duckdb import ObjectStoreClient, ObjectStoreStrategy
from ovid2.drivers.azure_blob.metadata import _parse_connection_string, resolve_address_auth_mode


def container_client(config: dict[str, Any]) -> Any:
    """Return an ``azure.storage.blob.ContainerClient`` for a merged config."""
    from azure.storage.blob import BlobServiceClient

    container = config.get("container")
    if not container:
        raise ValueError(
            "azure_blob address has no container (location.container / container_default)"
        )
    mode = resolve_address_auth_mode(config)
    if mode == "connection_string":
        service = BlobServiceClient.from_connection_string(str(config["connection_string"]))
        return service.get_container_client(str(container))
    account = config.get("account_name")
    if not account and config.get("connection_string"):
        account = _parse_connection_string(str(config["connection_string"])).get("AccountName")
    if not account:
        raise ValueError("azure_blob address has no account_name")
    suffix = str(config.get("endpoint_suffix") or "core.windows.net")
    url = f"https://{account}.blob.{suffix}"
    credential: Any
    if mode == "account_key":
        credential = {"account_name": account, "account_key": config["account_key"]}
    elif mode == "sas_token":
        credential = str(config["sas_token"]).lstrip("?")
    elif mode == "service_principal":
        from azure.identity import ClientSecretCredential

        credential = ClientSecretCredential(
            tenant_id=config["tenant_id"],
            client_id=config["client_id"],
            client_secret=config["client_secret"],
        )
    else:
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential()
    return BlobServiceClient(account_url=url, credential=credential).get_container_client(
        str(container)
    )


class AzureBlobClient(ObjectStoreClient):
    """Azure SDK-backed :class:`ObjectStoreClient`."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._container = container_client(config)

    def list_keys(self, prefix: str) -> list[str]:
        return [b.name for b in self._container.list_blobs(name_starts_with=prefix or None)]

    def download(self, key: str, dest: str) -> None:
        with open(dest, "wb") as fh:
            self._container.download_blob(key, max_concurrency=8).readinto(fh)

    def upload(self, src: str, key: str) -> None:
        with open(src, "rb") as fh:
            self._container.upload_blob(key, fh, overwrite=True, max_concurrency=8)

    def delete(self, keys: list[str]) -> None:
        for key in keys:
            try:
                self._container.delete_blob(key)
            except Exception as exc:
                if "BlobNotFound" not in str(exc):
                    raise


@register_duckdb_strategy
class DuckDBAzureBlobStrategy(ObjectStoreStrategy):
    """azure_blob <-> DuckDB."""

    driver_type = "azure_blob"

    def client(self, config: dict[str, Any]) -> ObjectStoreClient:
        return AzureBlobClient(config)
