"""ODS resolver — turn a path + dimension into an address to read/write with.

Public API:

- :func:`resolve` — the primary entry point.
- :func:`list_pipelines` / :func:`list_batches` / :func:`list_tables` —
  read-only listings used by the web tier + CLI + MCP tools.

The resolver deliberately does **not** perform any read or write. It
returns a :class:`ResolvedTarget` whose ``address`` field is a
:class:`~ovid2.core.addresses.base.BaseAddress` instance the caller
can then use. This keeps pyspark imports out of the resolver.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ovid2.config import addresses as _address_config
from ovid2.dataspace.path import OdsPath, OdsPathError
from ovid2.dataspace.registry import (
    dimension_for_address,
    list_addresses_by_dimension,
    list_dimensions,
)

if TYPE_CHECKING:
    from ovid2.core.addresses.base import BaseAddress

logger = logging.getLogger(__name__)

__all__ = [
    "OdsResolutionError",
    "ResolvedTarget",
    "list_batches",
    "list_pipelines",
    "list_tables",
    "resolve",
]


class OdsResolutionError(ValueError):
    """Raised when a ``(path, dim)`` pair cannot be routed to an address."""


@dataclass(frozen=True)
class ResolvedTarget:
    """The outcome of :func:`resolve`.

    ``address`` is instantiated lazily — pass ``materialise=False`` to
    :func:`resolve` when only the naming information is needed. The
    lazy path avoids pulling optional vendor drivers into memory just
    to answer "which config would handle this?".
    """

    path: OdsPath
    dimension: str
    address_name: str
    address: BaseAddress | None = None
    #: Which resolution tier fired: ``"explicit"``, ``"pipeline_route"``,
    #: ``"dimension_default"``, or ``"sole_candidate"``.
    tier: str = "sole_candidate"
    #: The candidate address names that were considered (for diagnostics).
    candidates: tuple[str, ...] = field(default_factory=tuple)


def _load_ods_settings() -> dict[str, Any]:
    """Read the ``settings.ods`` section, tolerating a missing file.

    Returns:
        A dict with keys ``default_dimension`` (str),
        ``dimension_defaults`` (dict), ``pipeline_routes``
        (dict[str, dict]), and ``pipeline_dimensions`` (dict).
        Missing keys are filled with sensible defaults.
    """
    # Import here to avoid a hard dep on the web layer from the resolver.
    try:
        from ovid2.web.services import settings_svc

        raw = settings_svc.get_settings().model_dump(mode="python")
    except Exception:  # noqa: BLE001 — settings are optional
        raw = {}
    section = raw.get("ods") or {}
    return {
        "default_dimension": str(section.get("default_dimension") or "disc"),
        "dimension_defaults": dict(section.get("dimension_defaults") or {}),
        "pipeline_routes": {
            str(k): dict(v or {})
            for k, v in (section.get("pipeline_routes") or {}).items()
        },
        "pipeline_dimensions": dict(section.get("pipeline_dimensions") or {}),
    }


def _pick_dimension(
    ods_path: OdsPath,
    dim: str | None,
    settings: dict[str, Any],
) -> str:
    """Choose the dimension for a path.

    Precedence:
        1. Explicit ``dim=`` argument.
        2. ``settings.ods.pipeline_dimensions[pipeline]`` (case-insensitive).
        3. ``settings.ods.default_dimension``.
    """
    if dim:
        if dim not in list_dimensions():
            raise OdsResolutionError(
                f"Unknown ODS dimension '{dim}'. Registered: {list_dimensions()}."
            )
        return dim

    if ods_path.pipeline is not None:
        by_pipeline = {k.lower(): v for k, v in settings["pipeline_dimensions"].items()}
        pipeline_default = by_pipeline.get(ods_path.pipeline.lower())
        if pipeline_default:
            if pipeline_default not in list_dimensions():
                raise OdsResolutionError(
                    f"Pipeline '{ods_path.pipeline}' maps to unknown dimension "
                    f"'{pipeline_default}' in settings.ods.pipeline_dimensions."
                )
            return str(pipeline_default)

    default = settings["default_dimension"]
    if default not in list_dimensions():
        raise OdsResolutionError(
            f"settings.ods.default_dimension '{default}' is not a registered dimension."
        )
    return str(default)


def _pick_address_name(
    ods_path: OdsPath,
    dimension: str,
    explicit_name: str | None,
    settings: dict[str, Any],
) -> tuple[str, str, tuple[str, ...]]:
    """Pick the address that should serve ``(path, dimension)``.

    Returns:
        ``(address_name, tier, candidates)``.

    Raises:
        OdsResolutionError: When no rule matches.
    """
    candidates = tuple(list_addresses_by_dimension(dimension))

    if explicit_name:
        # Sanity check — explicit override must exist and match the dimension.
        actual_dim = dimension_for_address(explicit_name)
        if actual_dim is None:
            raise OdsResolutionError(
                f"Explicit address '{explicit_name}' does not exist."
            )
        if actual_dim != dimension:
            raise OdsResolutionError(
                f"Explicit address '{explicit_name}' has dimension "
                f"'{actual_dim}', not '{dimension}'."
            )
        return explicit_name, "explicit", candidates

    if ods_path.pipeline is not None:
        pipeline_routes = {k.lower(): v for k, v in settings["pipeline_routes"].items()}
        per_pipeline = pipeline_routes.get(ods_path.pipeline.lower(), {})
        routed = per_pipeline.get(dimension)
        if routed:
            if routed not in candidates:
                raise OdsResolutionError(
                    f"Pipeline route for '{ods_path.pipeline}'/'{dimension}' points at "
                    f"address '{routed}' which is not registered under that dimension."
                )
            return str(routed), "pipeline_route", candidates

    dim_defaults = settings["dimension_defaults"]
    default_addr = dim_defaults.get(dimension)
    if default_addr:
        if default_addr not in candidates:
            raise OdsResolutionError(
                f"settings.ods.dimension_defaults['{dimension}'] points at "
                f"address '{default_addr}' which is not registered under that dimension."
            )
        return str(default_addr), "dimension_default", candidates

    if len(candidates) == 1:
        return candidates[0], "sole_candidate", candidates

    if not candidates:
        raise OdsResolutionError(
            f"No address configs registered under dimension '{dimension}'. "
            "Create one at /addresses/new (web UI) or via `ovid2 tools migrate-addresses`."
        )

    raise OdsResolutionError(
        f"Dimension '{dimension}' has {len(candidates)} candidate address(es): "
        f"{list(candidates)}. Add a per-pipeline or per-dimension default in "
        "settings.ods, or pass an explicit address name."
    )


def resolve(
    path: str | OdsPath,
    dim: str | None = None,
    *,
    address: str | None = None,
    pipeline: str | None = None,
    materialise: bool = True,
) -> ResolvedTarget:
    """Resolve an ODS path to a target address.

    Args:
        path: An :class:`OdsPath` or a POSIX-style string.
        dim: Optional dimension override. When ``None`` the default
            resolution (pipeline_dimensions → default_dimension) fires.
        address: Optional explicit address config name — bypasses the
            per-pipeline / per-dimension routing rules but still
            validates that the address's dimension matches ``dim`` (or
            the resolved default).
        pipeline: Optional pipeline override, used for Snowflake's
            four-tier config resolution. Falls back to ``path.pipeline``.
        materialise: When ``True`` (default) also instantiate the
            :class:`~ovid2.core.addresses.base.BaseAddress`. Callers
            that only need naming metadata should pass ``False`` — it
            avoids pulling optional vendor drivers.

    Returns:
        A :class:`ResolvedTarget`.

    Raises:
        OdsPathError: When ``path`` fails the ODS grammar.
        OdsResolutionError: When no address can be chosen.
    """
    ods_path = path if isinstance(path, OdsPath) else OdsPath.parse(path)

    if ods_path.pipeline is None and address is None:
        # Root-level resolution has no pipeline and no address to bind to;
        # the caller most likely wants `list_pipelines()` instead.
        raise OdsResolutionError(
            "Cannot resolve the root path — call list_pipelines() to enumerate children."
        )

    settings = _load_ods_settings()
    dimension = _pick_dimension(ods_path, dim, settings)
    address_name, tier, candidates = _pick_address_name(
        ods_path, dimension, address, settings
    )

    address_obj: BaseAddress | None = None
    if materialise:
        address_obj = _address_config.load_address(
            address_name,
            pipeline=pipeline or ods_path.pipeline,
        )

    return ResolvedTarget(
        path=ods_path,
        dimension=dimension,
        address_name=address_name,
        address=address_obj,
        tier=tier,
        candidates=candidates,
    )


# ---------------------------------------------------------------------------
# Read-only listings
# ---------------------------------------------------------------------------


def list_pipelines(dim: str | None = None) -> list[str]:
    """Return every pipeline discoverable under ODS.

    Args:
        dim: Optional dimension filter. When ``None``, the union of
            pipelines visible in *every* dimension is returned. When
            passed, only pipelines visible in that dimension are
            returned.

    Returns:
        Sorted, de-duplicated list of pipeline names. Missing address
        directories (e.g. the disc root doesn't exist yet on this
        host) contribute nothing rather than raising.
    """
    from ovid2.web.services import pipelines_meta_svc

    # Start with the package-declared pipeline roster — always ground
    # truth for "what pipelines does this deployment know about".
    known: set[str] = set(pipelines_meta_svc.discover_pipelines())

    if dim is None or dim == "disc":
        for address_name in list_addresses_by_dimension("disc"):
            for name in _list_pipelines_on_disc(address_name):
                known.add(name)
    return sorted(known)


def _list_pipelines_on_disc(address_name: str) -> list[str]:
    """Return top-level directory names under a disc address root."""
    try:
        target = resolve(
            OdsPath(pipeline="_placeholder"),
            dim="disc",
            address=address_name,
            materialise=True,
        )
    except OdsResolutionError:
        return []
    address = target.address
    # Late import to keep pyspark out of the resolver's default path.
    from ovid2.drivers.disc import DiscAddress

    if not isinstance(address, DiscAddress):
        return []
    root = Path(address.root_path)
    if not root.is_dir():
        return []
    return [entry.name for entry in sorted(root.iterdir()) if entry.is_dir()]


def list_batches(pipeline: str, dim: str | None = None) -> list[str]:
    """Return every batch under a pipeline.

    Non-disc dimensions currently return an empty list — see the ADR's
    *Follow-up work* section. The disc dimension walks
    ``{root_path}/{pipeline}/`` (with three-way case fallback matching
    :meth:`DiscAddress._get_read_path`).
    """
    if not pipeline:
        raise OdsPathError("list_batches requires a pipeline name.")

    dimension = dim or "disc"
    if dimension != "disc":
        return []

    from ovid2.drivers.disc import DiscAddress

    seen: set[str] = set()
    for address_name in list_addresses_by_dimension("disc"):
        try:
            target = resolve(
                OdsPath(pipeline=pipeline),
                dim="disc",
                address=address_name,
                materialise=True,
            )
        except OdsResolutionError:
            continue
        if not isinstance(target.address, DiscAddress):
            continue
        root = Path(target.address.root_path)
        for variant in (pipeline, pipeline.lower(), pipeline.upper()):
            candidate = root / variant
            if candidate.is_dir():
                for child in candidate.iterdir():
                    if child.is_dir():
                        seen.add(child.name)
                break
    return sorted(seen)


def list_tables(pipeline: str, batch_id: str, dim: str | None = None) -> list[str]:
    """Return every table under ``/pipeline/batch``.

    Uses the same three-way pipeline case fallback as
    :meth:`DiscAddress._get_read_path`. Non-disc dimensions return an
    empty list (see the ADR).
    """
    if not pipeline:
        raise OdsPathError("list_tables requires a pipeline name.")
    if not batch_id:
        raise OdsPathError("list_tables requires a batch_id.")

    dimension = dim or "disc"
    if dimension != "disc":
        return []

    from ovid2.drivers.disc import DiscAddress

    seen: set[str] = set()
    for address_name in list_addresses_by_dimension("disc"):
        try:
            target = resolve(
                OdsPath(pipeline=pipeline, batch_id=batch_id),
                dim="disc",
                address=address_name,
                materialise=True,
            )
        except OdsResolutionError:
            continue
        if not isinstance(target.address, DiscAddress):
            continue
        root = Path(target.address.root_path)
        for variant in (pipeline, pipeline.lower(), pipeline.upper()):
            candidate = root / variant / batch_id
            if candidate.is_dir():
                for entry in candidate.iterdir():
                    if entry.is_file():
                        # Strip a single extension so tables look logical.
                        stem = entry.stem
                        seen.add(stem)
                    elif entry.is_dir():
                        seen.add(entry.name)
                break
    return sorted(seen)
