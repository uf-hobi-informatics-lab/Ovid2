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
    args: dict[str, Any], secrets: dict[str, Any],
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
        import boto3  # type: ignore[import-not-found]
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
        session = boto3.session.Session(**session_kwargs)  # type: ignore[no-untyped-call]
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
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            ok=False,
            code="unreachable",
            message=f"S3 probe failed: {exc}",
            capability="auth",
        )


__all__ = ["list_dir_from_config", "probe_auth_from_config"]


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
        import boto3  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotImplementedError("boto3 is not installed on this host.") from exc

    bucket = location.get("bucket") or args.get("bucket_default")
    if not bucket:
        raise NotImplementedError(
            "No bucket set on the storage account (location.bucket or "
            "connection bucket_default)."
        )

    # Assemble the effective prefix: location.path (if any) + the
    # requested sub-prefix, normalised to a single trailing slash.
    base = str(location.get("path") or "").strip().strip("/")
    sub = (prefix or "").strip().strip("/")
    parts = [p for p in (base, sub) if p]
    effective = ("/".join(parts) + "/") if parts else ""

    session_kwargs: dict[str, Any] = {}
    for key, secret_key in (
        ("aws_access_key_id", "aws_access_key_id"),
        ("aws_secret_access_key", "aws_secret_access_key"),
        ("aws_session_token", "aws_session_token"),
    ):
        val = secrets.get(secret_key)
        if val:
            session_kwargs[key] = val
    if args.get("region"):
        session_kwargs["region_name"] = args["region"]

    client_kwargs: dict[str, Any] = {}
    if args.get("endpoint_url"):
        client_kwargs["endpoint_url"] = args["endpoint_url"]

    try:
        session = boto3.session.Session(**session_kwargs)  # type: ignore[no-untyped-call]
        client = session.client("s3", **client_kwargs)
        paginator = client.get_paginator("list_objects_v2")
        entries: list[StorageEntry] = []
        seen_dirs: set[str] = set()
        seen_files: set[str] = set()
        for page in paginator.paginate(
            Bucket=bucket, Prefix=effective, Delimiter="/"
        ):
            for cp in page.get("CommonPrefixes", []) or []:
                full = str(cp.get("Prefix", ""))
                name = full[len(effective):].rstrip("/")
                if not name or name in seen_dirs:
                    continue
                seen_dirs.add(name)
                # Path returned is relative to location.path so it
                # round-trips as the next ``prefix``.
                rel = full[len(base) + 1:].rstrip("/") if base else full.rstrip("/")
                entries.append(StorageEntry(name=name, kind="dir", path=rel))
            for obj in page.get("Contents", []) or []:
                full = str(obj.get("Key", ""))
                name = full[len(effective):]
                if not name or "/" in name or name in seen_files:
                    continue
                seen_files.add(name)
                rel = full[len(base) + 1:] if base else full
                entries.append(StorageEntry(name=name, kind="file", path=rel))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"S3 list failed: {exc}") from exc

    entries.sort(key=lambda e: (0 if e.kind == "dir" else 1, e.name.lower()))
    return entries
