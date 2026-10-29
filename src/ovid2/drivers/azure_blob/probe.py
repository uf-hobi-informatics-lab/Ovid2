"""Azure Blob driver probe helpers.

Uses :mod:`azure.storage.blob` and :mod:`azure.identity` lazily
(imports inside the function) so this module is import-safe: enumerating
drivers does not pull in the Azure SDK at module load time.

The heavy import happens only when someone actually runs the probe.

Kept in a dedicated module because the D3 recipe mandates the
four-file split (``metadata`` + ``driver`` + ``probe`` + ``spark``) so
contract tests can enforce import safety per file rather than per
method.
"""

from __future__ import annotations

from typing import Any

from ovid2.drivers.azure_blob.metadata import resolve_connection_auth_mode
from ovid2.drivers.base import ProbeResult

#: Per-call SDK timeout (seconds). Cap connection + read separately so
#: a stuck TCP handshake fails within one budget.
_PROBE_TIMEOUT_SECONDS: int = 8

#: Substrings we consider authentication-related when categorising an
#: Azure SDK exception into ``auth`` vs ``unreachable``.
#:
#: The ``Authorization*Mismatch`` family covers the case where the
#: identity authenticated successfully but was denied by an RBAC role
#: assignment, an IP allow-list, an HTTPS-only policy, or a resource-
#: type restriction. All of these are configuration problems on the
#: Azure side — the probe reached the service — so they belong under
#: ``auth`` rather than ``unreachable``. In particular
#: ``AuthorizationPermissionMismatch`` is what Azure returns when a
#: managed identity is missing ``Storage Blob Data Reader`` (or the
#: equivalent contributor role) on the target container.
_AUTH_HINTS: tuple[str, ...] = (
    "AuthenticationFailed",
    "AuthorizationFailure",
    "AuthorizationPermissionMismatch",
    "AuthorizationSourceIPMismatch",
    "AuthorizationProtocolMismatch",
    "AuthorizationResourceTypeMismatch",
    "InvalidAuthenticationInfo",
    "AADSTS",
    "ClientAuthenticationError",
    "Signature did not match",
    "Server failed to authenticate",
)


def _fast_fail_kwargs() -> dict[str, Any]:
    """Return the client-construction kwargs that disable retries.

    The Azure Storage SDK's default policy is 10 exponentially-backed-
    off retries, which turns a bad DNS lookup into a ~85-second stall
    inside the synchronous ``test()`` route. A probe is a cheap
    round-trip; there is no reason to retry it.
    """
    return {
        "retry_total": 0,
        "connection_timeout": _PROBE_TIMEOUT_SECONDS,
        "read_timeout": _PROBE_TIMEOUT_SECONDS,
    }


def _build_client(
    mode: str,
    args: dict[str, Any],
    secrets: dict[str, Any],
    url: str,
) -> Any:
    """Return a ready ``BlobServiceClient`` for the configured mode.

    Args:
        mode: One of ``connection_string`` / ``account_key`` /
            ``sas_token`` / ``service_principal`` / ``default_chain``.
        args: Non-secret connection args (``account_name``,
            ``container_default``, ``endpoint_suffix``).
        secrets: Credential dict.
        url: Pre-built ``https://<account>.blob.<suffix>`` URL
            (unused for connection-string auth, which carries its own
            endpoint).

    Raises:
        ImportError: When ``azure-storage-blob`` (or
            ``azure-identity`` for AAD modes) is not installed.
    """
    del args  # unused — kept for symmetry with the connection API.
    from azure.storage.blob import BlobServiceClient

    client_kwargs = _fast_fail_kwargs()
    if mode == "connection_string":
        return BlobServiceClient.from_connection_string(
            str(secrets["connection_string"]),
            **client_kwargs,
        )
    if mode == "account_key":
        return BlobServiceClient(
            account_url=url,
            credential=str(secrets["account_key"]),
            **client_kwargs,
        )
    if mode == "sas_token":
        # SAS may be passed either as the credential (portal form) or
        # appended to the URL as a query string — both work with
        # BlobServiceClient. We use the credential form for clarity.
        return BlobServiceClient(
            account_url=url,
            credential=str(secrets["sas_token"]),
            **client_kwargs,
        )
    # AAD-based modes require azure-identity.
    from azure.identity import (
        ClientSecretCredential,
        DefaultAzureCredential,
    )

    credential: ClientSecretCredential | DefaultAzureCredential
    if mode == "service_principal":
        credential = ClientSecretCredential(
            tenant_id=str(secrets["tenant_id"]),
            client_id=str(secrets["client_id"]),
            client_secret=str(secrets["client_secret"]),
        )
    else:  # default_chain
        credential = DefaultAzureCredential()
    return BlobServiceClient(
        account_url=url,
        credential=credential,
        **client_kwargs,
    )


def probe_auth_from_config(
    args: dict[str, Any],
    secrets: dict[str, Any],
) -> ProbeResult:
    """Return an ``auth`` probe result for an Azure Blob connection.

    Mirrors the pre-D3.azure_blob :meth:`AzureBlobConnection.test`
    behaviour verbatim:

    1. When ``azure-storage-blob`` is not installed, return
       ``code='skipped'``.
    2. Require ``account_name`` unless a connection string is used
       (which carries its own endpoint).
    3. For AAD-based modes, verify ``azure-identity`` is installed.
    4. When ``container_default`` is set, probe
       ``get_container_properties()`` — validates both account
       credentials and per-container ACLs.
    5. Otherwise fall back to ``get_account_information()``, which
       only needs a valid account-level credential.
    6. Any exception whose message contains an auth hint downgrades
       to ``code='auth'``; everything else downgrades to
       ``code='unreachable'``.

    Args:
        args: Non-secret connection args (``account_name``,
            ``container_default``, ``endpoint_suffix``).
        secrets: Credential dict.

    Returns:
        :class:`~ovid2.drivers.base.ProbeResult` with
        ``capability='auth'``.
    """
    try:
        import azure.storage.blob
    except ImportError:
        return ProbeResult(
            ok=False,
            code="skipped",
            message=(
                "azure-storage-blob is not installed on this host. "
                "Install with `pip install 'ovid2[azure]'`."
            ),
            capability="auth",
        )

    mode = resolve_connection_auth_mode(secrets)
    account = str(args.get("account_name") or "")
    suffix = str(args.get("endpoint_suffix") or "core.windows.net")

    # A connection string carries its own account + endpoint, so the
    # explicit ``account_name`` arg is optional in that one case.
    if not account and mode != "connection_string":
        return ProbeResult(
            ok=False,
            code="config",
            message=("account_name is required unless a connection_string secret is provided."),
            capability="auth",
        )

    if mode in ("service_principal", "default_chain"):
        try:
            import azure.identity  # noqa: F401 — import probe
        except ImportError:
            return ProbeResult(
                ok=False,
                code="skipped",
                message=(
                    "azure-identity is not installed on this host. "
                    "Install with `pip install 'ovid2[azure]'`."
                ),
                capability="auth",
            )

    url = f"https://{account}.blob.{suffix}" if account else ""
    container = args.get("container_default")

    try:
        client = _build_client(mode, args, secrets, url)
    except ValueError as exc:
        # Malformed connection string, invalid SPN, …
        return ProbeResult(
            ok=False,
            code="config",
            message=f"Azure client construction failed: {exc}",
            capability="auth",
            detail={"auth_mode": mode},
        )
    except Exception as exc:
        return ProbeResult(
            ok=False,
            code="config",
            message=f"Azure client construction failed: {exc}",
            capability="auth",
            detail={"auth_mode": mode},
        )

    target_url = getattr(client, "url", url) or url
    try:
        if container:
            container_client = client.get_container_client(str(container))
            container_client.get_container_properties(
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
            return ProbeResult(
                ok=True,
                code="ok",
                message=(f"get_container_properties() succeeded on {container}."),
                capability="auth",
                detail={
                    "account_url": target_url,
                    "container": str(container),
                    "auth_mode": mode,
                },
            )
        client.get_account_information(timeout=_PROBE_TIMEOUT_SECONDS)
        return ProbeResult(
            ok=True,
            code="ok",
            message=f"get_account_information() succeeded on {target_url}.",
            capability="auth",
            detail={"account_url": target_url, "auth_mode": mode},
        )
    except Exception as exc:
        code = "auth" if any(hint in str(exc) for hint in _AUTH_HINTS) else "unreachable"
        return ProbeResult(
            ok=False,
            code=code,
            message=f"Azure probe failed: {exc}",
            capability="auth",
            detail={"account_url": target_url, "auth_mode": mode},
        )


def list_dir_from_config(
    args: dict[str, Any],
    secrets: dict[str, Any],
    location: dict[str, Any],
    prefix: str | None,
) -> list[Any]:
    """List blob prefixes + blobs one level under ``prefix``.

    Azure Blob storage has a flat namespace; "directories" are a
    prefix convention. Using ``walk_blobs(delimiter='/')`` makes the
    SDK collapse everything below the next ``/`` into a ``BlobPrefix``
    marker, which we render as a ``dir`` entry (matching the s3
    driver's ``CommonPrefixes`` handling); leaf blobs at this level
    become ``file`` entries. ``azure-storage-blob`` is imported lazily.

    Args:
        args: Connection args (``account_name``, ``container_default``,
            ``endpoint_suffix``).
        secrets: Credential material (any supported auth mode).
        location: Address location (``container``, ``path``).
        prefix: Sub-prefix under the container root. ``None`` lists the
            container root (or ``location['path']`` when set).

    Returns:
        A list of :class:`~ovid2.drivers.base.StorageEntry`.

    Raises:
        NotImplementedError: When ``azure-storage-blob`` is absent or
            no container is resolvable (browsing is unavailable, not a
            failure — surfaces as ``supported=False``).
        ValueError: When the Azure API call fails.
    """
    from ovid2.drivers.base import StorageEntry

    try:
        from azure.storage.blob import (
            BlobPrefix,
        )
    except ImportError as exc:
        raise NotImplementedError("azure-storage-blob is not installed on this host.") from exc

    container = location.get("container") or args.get("container_default")
    if not container:
        raise NotImplementedError(
            "No container set on the storage account (location.container "
            "or connection container_default)."
        )

    mode = resolve_connection_auth_mode(dict(secrets))
    account = str(args.get("account_name") or "")
    suffix = str(args.get("endpoint_suffix") or "core.windows.net")
    url = f"https://{account}.blob.{suffix}" if account else ""

    # Assemble the effective prefix: location.path (if any) + the
    # requested sub-prefix, normalised to a single trailing slash.
    base = str(location.get("path") or "").strip().strip("/")
    sub = (prefix or "").strip().strip("/")
    parts = [p for p in (base, sub) if p]
    effective = ("/".join(parts) + "/") if parts else ""

    try:
        client = _build_client(mode, dict(args), dict(secrets), url)
        container_client = client.get_container_client(str(container))
        entries: list[StorageEntry] = []
        seen_dirs: set[str] = set()
        seen_files: set[str] = set()
        for item in container_client.walk_blobs(name_starts_with=effective or None, delimiter="/"):
            full = str(getattr(item, "name", ""))
            is_dir = isinstance(item, BlobPrefix) or full.endswith("/")
            if is_dir:
                name = full[len(effective) :].rstrip("/")
                if not name or name in seen_dirs:
                    continue
                seen_dirs.add(name)
                # Path returned is relative to location.path so it
                # round-trips as the next ``prefix``.
                rel = full[len(base) + 1 :].rstrip("/") if base else full.rstrip("/")
                entries.append(StorageEntry(name=name, kind="dir", path=rel))
            else:
                name = full[len(effective) :]
                if not name or "/" in name or name in seen_files:
                    continue
                seen_files.add(name)
                rel = full[len(base) + 1 :] if base else full
                size = getattr(item, "size", None)
                entries.append(
                    StorageEntry(
                        name=name,
                        kind="file",
                        path=rel,
                        size_bytes=int(size) if size is not None else None,
                        modified_at=_iso(getattr(item, "last_modified", None)),
                    )
                )
    except Exception as exc:
        raise ValueError(f"Azure list failed: {exc}") from exc

    entries.sort(key=lambda e: (0 if e.kind == "dir" else 1, e.name.lower()))
    return entries


def list_tables_from_config(
    args: dict[str, Any],
    secrets: dict[str, Any],
    location: dict[str, Any],
    sub_prefix: str,
) -> list[Any]:
    """List the immediate children of ``sub_prefix`` with *summed* sizes.

    Unlike :func:`list_dir_from_config` this walks every blob under the
    prefix (no delimiter) and aggregates by the first path segment, so a
    Spark-written ``<table>.csv/part-*`` directory reports the total
    size of its parts. Used for ``all`` expansion and largest-first
    copy scheduling — metadata only, no blob bodies are read.

    Args:
        args: Connection args.
        secrets: Credential material.
        location: Address location (``container``, ``path``).
        sub_prefix: Prefix under ``location.path`` (``pipeline/batch``).

    Returns:
        One :class:`~ovid2.drivers.base.StorageEntry` per child, with
        ``size_bytes`` set. An empty list when nothing is under the
        prefix.

    Raises:
        NotImplementedError: SDK absent / no container.
        ValueError: When the Azure API call fails.
    """
    from ovid2.drivers.base import StorageEntry

    try:
        import azure.storage.blob  # noqa: F401
    except ImportError as exc:
        raise NotImplementedError("azure-storage-blob is not installed on this host.") from exc

    container = location.get("container") or args.get("container_default")
    if not container:
        raise NotImplementedError(
            "No container set on the storage account (location.container "
            "or connection container_default)."
        )
    mode = resolve_connection_auth_mode(dict(secrets))
    account = str(args.get("account_name") or "")
    suffix = str(args.get("endpoint_suffix") or "core.windows.net")
    url = f"https://{account}.blob.{suffix}" if account else ""
    base = str(location.get("path") or "").strip().strip("/")
    sub = (sub_prefix or "").strip().strip("/")
    parts = [p for p in (base, sub) if p]
    effective = ("/".join(parts) + "/") if parts else ""

    totals: dict[str, int] = {}
    is_dir: dict[str, bool] = {}
    try:
        client = _build_client(mode, dict(args), dict(secrets), url)
        container_client = client.get_container_client(str(container))
        for blob in container_client.list_blobs(name_starts_with=effective or None):
            rest = str(getattr(blob, "name", ""))[len(effective) :]
            if not rest:
                continue
            head, sep, _tail = rest.partition("/")
            totals[head] = totals.get(head, 0) + int(getattr(blob, "size", 0) or 0)
            is_dir[head] = is_dir.get(head, False) or bool(sep)
    except Exception as exc:
        raise ValueError(f"Azure list failed: {exc}") from exc
    return [
        StorageEntry(
            name=name,
            kind="dir" if is_dir[name] else "file",
            path=f"{sub}/{name}" if sub else name,
            size_bytes=totals[name],
        )
        for name in sorted(totals, key=str.lower)
    ]


def _iso(value: Any) -> str | None:
    """Render an SDK ``datetime`` as ISO-8601 UTC (``None`` passes)."""
    from ovid2.drivers._tabular import iso_utc

    return iso_utc(value) if value is not None else None


def tabular_source_from_config(
    args: dict[str, Any],
    secrets: dict[str, Any],
    location: dict[str, Any],
) -> Any:
    """Return an :class:`~ovid2.drivers._tabular.ObjectStoreSource` over the container.

    Paths are blob names relative to ``location.path`` (the same paths
    :func:`list_dir_from_config` returns). Blobs are read with ranged
    ``download_blob(offset, length)``.

    Raises:
        NotImplementedError: SDK absent / no container.
    """
    from ovid2.drivers._tabular import ObjectStoreSource

    try:
        from azure.core.exceptions import ResourceNotFoundError
        from azure.storage.blob import BlobPrefix
    except ImportError as exc:
        raise NotImplementedError("azure-storage-blob is not installed on this host.") from exc
    container = location.get("container") or args.get("container_default")
    if not container:
        raise NotImplementedError(
            "No container set on the storage account (location.container "
            "or connection container_default)."
        )
    mode = resolve_connection_auth_mode(dict(secrets))
    account = str(args.get("account_name") or "")
    suffix = str(args.get("endpoint_suffix") or "core.windows.net")
    url = f"https://{account}.blob.{suffix}" if account else ""
    client = _build_client(mode, dict(args), dict(secrets), url)
    container_client = client.get_container_client(str(container))

    def head(key: str) -> tuple[int, str | None] | None:
        try:
            props = container_client.get_blob_client(key).get_blob_properties()
        except ResourceNotFoundError:
            return None
        return int(props.size or 0), _iso(props.last_modified)

    def listing(prefix: str) -> tuple[list[str], list[tuple[str, int, str | None]]]:
        dirs: list[str] = []
        files: list[tuple[str, int, str | None]] = []
        for item in container_client.walk_blobs(name_starts_with=prefix or None, delimiter="/"):
            full = str(getattr(item, "name", ""))
            name = full[len(prefix) :]
            if isinstance(item, BlobPrefix) or full.endswith("/"):
                name = name.rstrip("/")
                if name:
                    dirs.append(name)
            elif name and "/" not in name:
                files.append(
                    (
                        name,
                        int(getattr(item, "size", 0) or 0),
                        _iso(getattr(item, "last_modified", None)),
                    )
                )
        return dirs, files

    def fetch(key: str, start: int, end: int) -> bytes:
        if end <= start:
            return b""
        downloader = container_client.download_blob(key, offset=start, length=end - start)
        return bytes(downloader.readall())

    return ObjectStoreSource(
        head=head, listing=listing, fetch=fetch, base=str(location.get("path") or "")
    )


__all__ = [
    "_AUTH_HINTS",
    "_PROBE_TIMEOUT_SECONDS",
    "_build_client",
    "_fast_fail_kwargs",
    "list_dir_from_config",
    "list_tables_from_config",
    "probe_auth_from_config",
    "tabular_source_from_config",
]
