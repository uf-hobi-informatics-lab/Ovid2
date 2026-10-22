"""Branding asset route.

Streams packaged Ovid brand marks (SVG + PNG variants) out of the wheel
so the control-panel header, favicon, and email digests all share a
single source of truth (see :mod:`ovid2.resources.branding`). The
alternative — copying assets into ``src/ovid2/web/static/`` — would fork
each asset and force a two-step update whenever the mark changes.

The route is deliberately excluded from the OpenAPI schema
(``include_in_schema=False``) because it behaves like a static asset,
not a JSON API surface, and its inclusion would churn the snapshot test.

Only names on the internal allow-list are served; anything else returns
404. The web control panel v2 uses the SVG version as both the topbar
logo and the favicon — because the SVG is wrapped in a
``viewBox="0 0 256 256"``, the browser can never accidentally render it
at native pixel size (the bug that motivated the redesign).

Assets served:

- ``ovid_logo.svg`` — SVG wrapper around the packaged raster. Used by
  the topbar, favicon, and any Retina-aware consumer.
- ``ovid_logo.png`` — 1254 × 1254 master, kept for legacy consumers.
- ``ovid_logo_32.png`` / ``_64.png`` / ``_128.png`` — pre-rendered small
  variants. Used by consumers that cannot render SVG.
- ``favicon.png`` / ``favicon.svg`` — aliases pointing at the small
  raster and the SVG respectively.

Caching policy
==============

Responses carry a strong ``ETag`` (MD5 of the payload) and a short
``Cache-Control: public, max-age=300`` window (five minutes). The short
TTL matters: if the running server is booted before the on-disk assets
are updated, its module-level ``_CACHE`` (populated at import time)
holds the old bytes. Combined with a long browser-side ``max-age`` this
would keep a broken logo visible for up to a day. Keeping the TTL small
means a stale response self-heals within minutes, while the ETag still
gives us cheap ``304 Not Modified`` revalidations for the happy path.
"""

from __future__ import annotations

import hashlib
from importlib.resources import files

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

router: APIRouter = APIRouter()

#: Media type for each served filename.
_MEDIA_TYPES: dict[str, str] = {
    "ovid_logo.svg": "image/svg+xml",
    "ovid_logo.png": "image/png",
    "ovid_logo_32.png": "image/png",
    "ovid_logo_64.png": "image/png",
    "ovid_logo_128.png": "image/png",
    "favicon.svg": "image/svg+xml",
    "favicon.png": "image/png",
}

#: Allow-list of served filenames → the underlying packaged resource.
_ASSETS: dict[str, str] = {
    "ovid_logo.svg": "ovid_logo.svg",
    "ovid_logo.png": "ovid_logo.png",
    "ovid_logo_32.png": "ovid_logo_32.png",
    "ovid_logo_64.png": "ovid_logo_64.png",
    "ovid_logo_128.png": "ovid_logo_128.png",
    "favicon.svg": "ovid_logo.svg",
    "favicon.png": "ovid_logo_32.png",
}


#: Browser cache TTL for branding assets, in seconds. Kept intentionally
#: small so that if the running server was booted before an on-disk
#: asset update, the resulting stale response self-heals within minutes
#: instead of a full day. The strong ETag still gives us cheap
#: revalidation for the common (unchanged) case.
_CACHE_MAX_AGE_SECONDS: int = 300


def _load(resource_name: str) -> bytes:
    """Read a packaged branding asset into memory.

    Args:
        resource_name: The filename inside the ``ovid2.resources.branding``
            package.

    Returns:
        The raw bytes of the packaged asset.
    """
    return files("ovid2.resources.branding").joinpath(resource_name).read_bytes()


#: One-time cache of the packaged asset bytes, plus their strong ETags
#: (MD5 of the payload, quoted per RFC 7232). Keeping everything resident
#: avoids a filesystem read per request; each asset is well under 100 KB.
_CACHE: dict[str, bytes] = {name: _load(src) for name, src in _ASSETS.items()}
_ETAGS: dict[str, str] = {
    # MD5 is used purely as a content-hash for ETag construction, not
    # for authentication or integrity signing. Marking
    # ``usedforsecurity=False`` silences ruff's S324 check.
    name: '"' + hashlib.md5(payload, usedforsecurity=False).hexdigest() + '"'
    for name, payload in _CACHE.items()
}


@router.get(
    "/branding/{filename}",
    include_in_schema=False,
)
def branding_asset(filename: str, request: Request) -> Response:
    """Serve a branding asset by filename.

    Args:
        filename: One of the entries in :data:`_ASSETS`.
        request: The incoming request, inspected for the
            ``If-None-Match`` conditional header to serve ``304 Not
            Modified`` on cache hits.

    Returns:
        A ``Response`` with the asset bytes, the right ``Content-Type``,
        a short cache header, and a strong ``ETag``. Returns
        ``304 Not Modified`` if the client already has the asset.

    Raises:
        HTTPException: 404 if ``filename`` is not on the allow-list.
    """
    payload = _CACHE.get(filename)
    if payload is None:
        raise HTTPException(status_code=404, detail="unknown branding asset")

    etag = _ETAGS[filename]
    cache_control = f"public, max-age={_CACHE_MAX_AGE_SECONDS}"
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": cache_control},
        )
    return Response(
        content=payload,
        media_type=_MEDIA_TYPES[filename],
        headers={
            "ETag": etag,
            "Cache-Control": cache_control,
        },
    )


if __name__ == "__main__":  # pragma: no cover - module is import-only
    raise SystemExit("ovid2.web.routes.branding is not directly runnable")
