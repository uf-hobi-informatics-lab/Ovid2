"""S3-driver probe helpers.

Uses :mod:`boto3` lazily (import inside the function) so this module
is import-safe: ``from ovid2.drivers.s3.probe import probe_auth`` never
pulls in botocore at import time. The heavy load happens only when
someone actually runs the probe.

Kept in a dedicated module because the D3 recipe mandates the
four-file split (``metadata`` + ``driver`` + ``probe`` + ``spark``)
so contract tests can enforce import safety per file rather than per
method.
"""

from __future__ import annotations

from typing import Any

from ovid2.drivers.base import ProbeResult


def probe_auth_from_config(
    args: dict[str, Any],
    secrets: dict[str, Any],
) -> ProbeResult:
    """Return an ``auth`` probe result for an S3 connection.

    Mirrors the pre-D3.s3 :meth:`S3Connection.test` behaviour:

    1. When ``boto3`` is not installed, return a ``skipped`` result.
       No wheel is pinned by Ovid2 today.
    2. Build a ``boto3.session.Session`` from the credentials +
       region.
    3. When ``bucket_default`` is set, issue ``head_bucket``. When
       absent, fall back to ``list_buckets`` (the cheaper of the two
       account-level probes).
    4. Any exception downgrades to ``code='unreachable'``.

    Args:
        args: Non-secret connection args (``region``, ``endpoint_url``,
            ``bucket_default``, ``signature_version``).
        secrets: Credential material (``aws_access_key_id``,
            ``aws_secret_access_key``, ``aws_session_token``).

    Returns:
        :class:`~ovid2.drivers.base.ProbeResult` with
        ``capability='auth'``.
    """
    try:
        import boto3
    except ImportError:
        return ProbeResult(
            ok=False,
            code="skipped",
            message="boto3 is not installed on this host.",
            capability="auth",
        )

    session_kwargs: dict[str, Any] = {}
    aki = secrets.get("aws_access_key_id")
    ask = secrets.get("aws_secret_access_key")
    ast_ = secrets.get("aws_session_token")
    if aki:
        session_kwargs["aws_access_key_id"] = aki
    if ask:
        session_kwargs["aws_secret_access_key"] = ask
    if ast_:
        session_kwargs["aws_session_token"] = ast_
    region = args.get("region")
    if region:
        session_kwargs["region_name"] = region

    try:
        session = boto3.session.Session(**session_kwargs)
        client_kwargs: dict[str, Any] = {}
        if args.get("endpoint_url"):
            client_kwargs["endpoint_url"] = args["endpoint_url"]
        client = session.client("s3", **client_kwargs)
        bucket = args.get("bucket_default")
        if bucket:
            client.head_bucket(Bucket=bucket)
            return ProbeResult(
                ok=True,
                code="ok",
                message=f"HEAD s3://{bucket} succeeded.",
                capability="auth",
                detail={"bucket": bucket},
            )
        client.list_buckets()
        return ProbeResult(
            ok=True,
            code="ok",
            message="list_buckets() succeeded.",
            capability="auth",
        )
    except Exception as exc:
        return ProbeResult(
            ok=False,
            code="unreachable",
            message=f"S3 probe failed: {exc}",
            capability="auth",
        )


__all__ = [
    "list_dir_from_config",
    "list_tables_from_config",
    "probe_auth_from_config",
    "tabular_source_from_config",
]


def _iso(value: Any) -> str | None:
    """Render a boto3 ``datetime`` as ISO-8601 UTC (``None`` passes)."""
    from ovid2.drivers._tabular import iso_utc

    return iso_utc(value) if value is not None else None


def list_dir_from_config(
    args: dict[str, Any],
    secrets: dict[str, Any],
    location: dict[str, Any],
    prefix: str | None,
) -> list[Any]:
    """List S3 prefixes + objects one level under ``prefix``.

    Uses ``list_objects_v2`` with ``Delimiter='/'`` so the response
    separates *common prefixes* (rendered as ``dir`` entries) from
    *objects* (rendered as ``file`` entries). boto3 is imported lazily.

    Args:
        args: Connection args (``region``, ``endpoint_url``,
            ``bucket_default``, ``signature_version``).
        secrets: AWS credential material.
        location: Address location (``bucket``, ``path``).
        prefix: Sub-prefix under the bucket root. ``None`` lists the
            bucket root (or ``location['path']`` when set).

    Returns:
        A list of :class:`~ovid2.drivers.base.StorageEntry`.

    Raises:
        NotImplementedError: When boto3 is absent or no bucket is
            resolvable (browsing is unavailable, not a failure).
        ValueError: When the S3 API call fails.
    """
    from ovid2.drivers.base import StorageEntry

    try:
        import boto3
    except ImportError as exc:
        raise NotImplementedError("boto3 is not installed on this host.") from exc

    bucket = location.get("bucket") or args.get("bucket_default")
    if not bucket:
        raise NotImplementedError(
            "No bucket set on the storage account (location.bucket or connection bucket_default)."
        )

    # Assemble the effective prefix: location.path (if any) + the
    # requested sub-prefix, normalised to a single trailing slash.
    base = str(location.get("path") or "").strip().strip("/")
    sub = (prefix or "").strip().strip("/")
    parts = [p for p in (base, sub) if p]
    effective = ("/".join(parts) + "/") if parts else ""

    try:
        client = _build_s3_client(boto3, args, secrets)
        paginator = client.get_paginator("list_objects_v2")
        entries: list[StorageEntry] = []
        seen_dirs: set[str] = set()
        seen_files: set[str] = set()
        for page in paginator.paginate(Bucket=bucket, Prefix=effective, Delimiter="/"):
            for cp in page.get("CommonPrefixes", []) or []:
                full = str(cp.get("Prefix", ""))
                name = full[len(effective) :].rstrip("/")
                if not name or name in seen_dirs:
                    continue
                seen_dirs.add(name)
                # Path returned is relative to location.path so it
                # round-trips as the next ``prefix``.
                rel = full[len(base) + 1 :].rstrip("/") if base else full.rstrip("/")
                entries.append(StorageEntry(name=name, kind="dir", path=rel))
            for obj in page.get("Contents", []) or []:
                full = str(obj.get("Key", ""))
                name = full[len(effective) :]
                if not name or "/" in name or name in seen_files:
                    continue
                seen_files.add(name)
                rel = full[len(base) + 1 :] if base else full
                size = obj.get("Size")
                entries.append(
                    StorageEntry(
                        name=name,
                        kind="file",
                        path=rel,
                        size_bytes=int(size) if size is not None else None,
                        modified_at=_iso(obj.get("LastModified")),
                    )
                )
    except Exception as exc:
        raise ValueError(f"S3 list failed: {exc}") from exc

    entries.sort(key=lambda e: (0 if e.kind == "dir" else 1, e.name.lower()))
    return entries


def _build_s3_client(boto3: Any, args: dict[str, Any], secrets: dict[str, Any]) -> Any:
    """Return a boto3 S3 client from connection ``args`` + ``secrets``."""
    session_kwargs: dict[str, Any] = {}
    for key in ("aws_access_key_id", "aws_secret_access_key", "aws_session_token"):
        val = secrets.get(key)
        if val:
            session_kwargs[key] = val
    if args.get("region"):
        session_kwargs["region_name"] = args["region"]
    client_kwargs: dict[str, Any] = {}
    if args.get("endpoint_url"):
        client_kwargs["endpoint_url"] = args["endpoint_url"]
    session = boto3.session.Session(**session_kwargs)
    return session.client("s3", **client_kwargs)


def list_tables_from_config(
    args: dict[str, Any],
    secrets: dict[str, Any],
    location: dict[str, Any],
    sub_prefix: str,
) -> list[Any]:
    """List the immediate children of ``sub_prefix`` with *summed* object sizes.

    Walks every key under the prefix (no delimiter) and aggregates by
    the first path segment, so a Spark-written ``<table>.csv/part-*``
    prefix reports the total of its parts. Metadata only.

    Args:
        args: Connection args.
        secrets: AWS credential material.
        location: Address location (``bucket``, ``path``).
        sub_prefix: Prefix under ``location.path`` (``pipeline/batch``).

    Returns:
        One :class:`~ovid2.drivers.base.StorageEntry` per child, with
        ``size_bytes`` set.

    Raises:
        NotImplementedError: boto3 absent / no bucket.
        ValueError: When the S3 API call fails.
    """
    from ovid2.drivers.base import StorageEntry

    try:
        import boto3
    except ImportError as exc:
        raise NotImplementedError("boto3 is not installed on this host.") from exc

    bucket = location.get("bucket") or args.get("bucket_default")
    if not bucket:
        raise NotImplementedError(
            "No bucket set on the storage account (location.bucket or connection bucket_default)."
        )
    base = str(location.get("path") or "").strip().strip("/")
    sub = (sub_prefix or "").strip().strip("/")
    parts = [p for p in (base, sub) if p]
    effective = ("/".join(parts) + "/") if parts else ""

    totals: dict[str, int] = {}
    is_dir: dict[str, bool] = {}
    try:
        client = _build_s3_client(boto3, args, secrets)
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=effective):
            for obj in page.get("Contents", []) or []:
                rest = str(obj.get("Key", ""))[len(effective) :]
                if not rest:
                    continue
                head, sep, _tail = rest.partition("/")
                totals[head] = totals.get(head, 0) + int(obj.get("Size", 0) or 0)
                is_dir[head] = is_dir.get(head, False) or bool(sep)
    except Exception as exc:
        raise ValueError(f"S3 list failed: {exc}") from exc
    return [
        StorageEntry(
            name=name,
            kind="dir" if is_dir[name] else "file",
            path=f"{sub}/{name}" if sub else name,
            size_bytes=totals[name],
        )
        for name in sorted(totals, key=str.lower)
    ]


def tabular_source_from_config(
    args: dict[str, Any],
    secrets: dict[str, Any],
    location: dict[str, Any],
) -> Any:
    """Return an :class:`~ovid2.drivers._tabular.ObjectStoreSource` over the bucket.

    Paths are keys relative to ``location.path`` — the same paths
    :func:`list_dir_from_config` returns. Objects are read with ranged
    ``GetObject`` so parquet footers / CSV headers never download the
    whole object.

    Raises:
        NotImplementedError: boto3 absent / no bucket.
    """
    from ovid2.drivers._tabular import ObjectStoreSource

    try:
        import boto3
    except ImportError as exc:
        raise NotImplementedError("boto3 is not installed on this host.") from exc
    bucket = location.get("bucket") or args.get("bucket_default")
    if not bucket:
        raise NotImplementedError(
            "No bucket set on the storage account (location.bucket or connection bucket_default)."
        )
    client = _build_s3_client(boto3, args, secrets)

    def head(key: str) -> tuple[int, str | None] | None:
        try:
            meta = client.head_object(Bucket=bucket, Key=key)
        except Exception as exc:
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return int(meta.get("ContentLength") or 0), _iso(meta.get("LastModified"))

    def listing(prefix: str) -> tuple[list[str], list[tuple[str, int, str | None]]]:
        dirs: list[str] = []
        files: list[tuple[str, int, str | None]] = []
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
            for cp in page.get("CommonPrefixes", []) or []:
                name = str(cp.get("Prefix", ""))[len(prefix) :].rstrip("/")
                if name:
                    dirs.append(name)
            for obj in page.get("Contents", []) or []:
                name = str(obj.get("Key", ""))[len(prefix) :]
                if name and "/" not in name:
                    files.append((name, int(obj.get("Size") or 0), _iso(obj.get("LastModified"))))
        return dirs, files

    def fetch(key: str, start: int, end: int) -> bytes:
        if end <= start:
            return b""
        body = client.get_object(Bucket=bucket, Key=key, Range=f"bytes={start}-{end - 1}")["Body"]
        return bytes(body.read())

    return ObjectStoreSource(
        head=head, listing=listing, fetch=fetch, base=str(location.get("path") or "")
    )
