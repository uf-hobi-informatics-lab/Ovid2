"""Run-kind registry — the single source of truth for pipeline *kinds*.

A **run kind** (``etl``, ``analysis``, ``dq_checks``, ``health``,
``copy``, ``move``) is not a cosmetic label: it carries executor
behaviour — which CLI subcommand the worker invokes, which argv builder
shapes the command line, whether the container mounts ``/data``
read-only, whether an installed ``ovid2_pipelines.<name>`` package is
required, and which per-kind fields (transformations / cdm / tables /
extra_mounts / strict_types / health-selection) are meaningful.

Historically every one of those decisions lived as a scattered
``if kind == "copy": ...`` branch across
:mod:`ovid2.web.services.pipelines_svc`,
:mod:`ovid2.web.services.jobs_svc`, :mod:`ovid2.web.worker`,
:mod:`ovid2.core.docker`, and :mod:`ovid2.web.routes.html`. This module
collapses them into one declarative :class:`KindSpec` per kind so that
adding a new run kind is a single registry entry plus (if it needs one)
a new CLI subcommand — the Pipelines page grouping, the New-pipeline
dropdown, create/submit validation, the worker argv dispatch, and the
container mount rule all follow automatically.

Import safety
-------------
This module lives at the top level of the package (like
:mod:`ovid2.connections.registry` and :mod:`ovid2.drivers.registry`) so
that **both** the trusted executor (:mod:`ovid2.core`, :mod:`ovid2.cli`)
**and** the web tier (:mod:`ovid2.web`) may import it. It has no heavy
dependencies and imports nothing from ``ovid2.core`` /
``ovid2.web`` — keeping ``tests/contract/test_web_no_pipeline_imports.py``
green.

Widening the registry is deliberate: the frozen tripwire in
``tests/contract/test_route_kind_valid.py`` and the completeness check in
``tests/contract/test_run_kind_registry_complete.py`` both force an
author to update an ADR and the tests when a kind is added.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "KIND_ALIASES",
    "KIND_REGISTRY",
    "ArgvBuilder",
    "CreateFlow",
    "KindSpec",
    "TablesMode",
    "all_kinds",
    "get_kind",
    "kind_for_command",
    "normalize_kind",
    "package_free_kinds",
]

#: How the worker/CLI shapes the container argv for a kind.
ArgvBuilder = Literal["etl", "health", "copy", "move"]

#: How the ``tables`` field is interpreted for a kind.
#:
#: - ``all``      — the ``["all"]`` sentinel is allowed (etl / analysis).
#: - ``explicit`` — an explicit list is required; ``["all"]`` is rejected
#:                  (copy / move — ``ovid2 tools {copy,move}`` use
#:                  ``nargs='+'`` and have no ``all`` sentinel).
#: - ``none``     — the kind has no formatter chain / tables are
#:                  meaningless and scrubbed (health / dq_checks).
TablesMode = Literal["all", "explicit", "none"]

#: Which New-pipeline create flow the web builder runs for a kind.
#:
#: - ``scaffold_package`` — scaffold a fresh ``ovid2_pipelines.<name>``
#:                          package from a template (etl).
#: - ``pick_package``     — bind ``pipeline`` to an existing installed
#:                          package chosen on the form (analysis).
#: - ``cycle_packages``   — require ≥1 EDC report-cycle selection;
#:                          ``pipeline`` stays the config label
#:                          (dq_checks).
#: - ``plain``            — no scaffolding, ``pipeline == name``
#:                          (health / copy / move).
CreateFlow = Literal["scaffold_package", "pick_package", "cycle_packages", "plain"]


@dataclass(frozen=True)
class KindSpec:
    """Declarative behaviour of one pipeline run kind.

    Every field replaces a former ``if kind == ...`` branch. See the
    module docstring for the provenance of each concern.

    Attributes:
        name: Canonical kind name (matches the registry key).
        label: Human-facing label for the dropdown / page group header.
        description: One-line description shown in the builder.
        package_required: When ``True`` the executor imports an
            ``ovid2_pipelines.<name>`` package, so create/submit require
            it to be installed. ``False`` for package-free kinds whose
            ``pipeline`` field is only a name/label.
        cli_subcommand: The leading ``ovid2`` subcommand token(s) the
            worker/CLI dispatch on (e.g. ``etl``, ``tools copy``).
        argv_builder: Which argv-shaping strategy the worker uses.
        uses_transformations: When ``False`` the formatter/mapper/fixer
            chain is bypassed and ``transformations`` is scrubbed.
        uses_cdm: When ``False`` a stale ``cdm`` value is scrubbed.
        tables_mode: How the ``tables`` field is interpreted.
        allows_extra_mounts: When ``True`` the kind accepts operator
            ``extra_mounts`` (copy / move only).
        allows_strict_types: When ``True`` the kind accepts the
            ``strict_types`` flag (move only).
        health_selection: When ``True`` the kind carries
            ``parts`` / ``packages`` / ``checks`` /
            ``prev_input_address`` health-selection fields.
        read_only_data_mount: When ``True`` the container mounts
            ``/data`` read-only. ``False`` for kinds that legitimately
            write under ``/data`` (copy / move) or refresh it (health).
        create_flow: Which New-pipeline create flow the web builder runs.
    """

    name: str
    label: str
    description: str
    package_required: bool
    cli_subcommand: str
    argv_builder: ArgvBuilder
    uses_transformations: bool
    uses_cdm: bool
    tables_mode: TablesMode
    allows_extra_mounts: bool = False
    allows_strict_types: bool = False
    health_selection: bool = False
    read_only_data_mount: bool = True
    create_flow: CreateFlow = "plain"

    @property
    def command_tokens(self) -> tuple[str, ...]:
        """Return :attr:`cli_subcommand` split into argv tokens.

        ``"tools copy"`` → ``("tools", "copy")``; ``"etl"`` → ``("etl",)``.
        """
        return tuple(self.cli_subcommand.split())


#: Deprecated kind names accepted on input and mapped to a canonical
#: registry key. ``cdm`` was renamed to ``etl`` in the pipeline-centric
#: panel refactor; every on-disk pipeline JSON and external caller
#: passing ``cdm`` keeps working via :func:`normalize_kind`.
KIND_ALIASES: dict[str, str] = {"cdm": "etl"}


#: The single source of truth for run kinds. Ordered — the Pipelines page
#: groups configs in this order.
KIND_REGISTRY: dict[str, KindSpec] = {
    "etl": KindSpec(
        name="etl",
        label="ETL",
        description=(
            "Full format → dedup → map → fix → upload pipeline gated by a "
            "CDM. Scaffolds a pipeline package from a template."
        ),
        package_required=True,
        cli_subcommand="etl",
        argv_builder="etl",
        uses_transformations=True,
        uses_cdm=True,
        tables_mode="all",
        read_only_data_mount=True,
        create_flow="scaffold_package",
    ),
    "analysis": KindSpec(
        name="analysis",
        label="Analysis",
        description=(
            "Read-only analysis over an existing installed pipeline "
            "package (runs the format stage / per-table formatters)."
        ),
        package_required=True,
        cli_subcommand="analysis",
        argv_builder="etl",
        uses_transformations=True,
        uses_cdm=False,
        tables_mode="all",
        read_only_data_mount=True,
        create_flow="pick_package",
    ),
    "dq_checks": KindSpec(
        name="dq_checks",
        label="DQ checks",
        description=(
            "EDC report-cycle data-quality checks. No formatter chain; "
            "select one or more report cycles."
        ),
        package_required=False,
        cli_subcommand="dq_checks",
        argv_builder="health",
        uses_transformations=False,
        uses_cdm=True,
        tables_mode="none",
        health_selection=True,
        read_only_data_mount=True,
        create_flow="cycle_packages",
    ),
    "health": KindSpec(
        name="health",
        label="Health",
        description=(
            "Data-health checks against a CDM. No formatter chain; may "
            "refresh /data, so the input tree is mounted read-write."
        ),
        package_required=False,
        cli_subcommand="health",
        argv_builder="health",
        uses_transformations=False,
        uses_cdm=True,
        tables_mode="none",
        health_selection=True,
        # health may refresh /data (see core/docker.py historical
        # behaviour: 'health' is in the rw set), so NOT read-only.
        read_only_data_mount=False,
        create_flow="plain",
    ),
    "copy": KindSpec(
        name="copy",
        label="Copy",
        description=(
            "One-shot address-to-address copy that bypasses the formatter / mapper / fixer stages."
        ),
        package_required=False,
        cli_subcommand="tools copy",
        argv_builder="copy",
        uses_transformations=False,
        uses_cdm=False,
        tables_mode="explicit",
        allows_extra_mounts=True,
        # copy writes to its destination address (disc destinations live
        # under /data), so /data must be read-write.
        read_only_data_mount=False,
        create_flow="plain",
    ),
    "move": KindSpec(
        name="move",
        label="Move",
        description=(
            "Driver-native successor to copy — same shape, routes I/O "
            "through native drivers and supports strict CDM type checks."
        ),
        package_required=False,
        cli_subcommand="tools move",
        argv_builder="move",
        uses_transformations=False,
        uses_cdm=False,
        tables_mode="explicit",
        allows_extra_mounts=True,
        allows_strict_types=True,
        read_only_data_mount=False,
        create_flow="plain",
    ),
}


def normalize_kind(kind: str | None) -> str:
    """Return the canonical registry key for ``kind``.

    Empty / ``None`` defaults to ``etl``. The deprecated ``cdm`` alias
    maps to ``etl``; every other value is returned lower-cased and
    stripped. Membership in :data:`KIND_REGISTRY` is validated at the
    call sites that need it (create/submit), not here.

    Args:
        kind: The raw kind value from a config, form, or API payload.

    Returns:
        The canonical, lower-cased kind name.
    """
    raw = (kind or "").strip().lower() or "etl"
    return KIND_ALIASES.get(raw, raw)


def all_kinds() -> tuple[str, ...]:
    """Return every registered kind name, in registry (display) order."""
    return tuple(KIND_REGISTRY.keys())


def package_free_kinds() -> frozenset[str]:
    """Return the kinds whose executor never imports a pipeline package."""
    return frozenset(name for name, spec in KIND_REGISTRY.items() if not spec.package_required)


def get_kind(kind: str | None) -> KindSpec | None:
    """Return the :class:`KindSpec` for ``kind`` (after normalisation), or ``None``."""
    return KIND_REGISTRY.get(normalize_kind(kind))


def kind_for_command(command: str | None) -> KindSpec | None:
    """Return the :class:`KindSpec` whose subcommand matches ``command``.

    Accepts both the two-token form (``"tools copy"``) and the bare kind
    name (``"copy"``) so the host-side docker/compose dispatch — which
    receives ``args.command`` — can resolve the spec. Returns ``None``
    for an unknown command (callers fall back to the etl shape).
    """
    if not command:
        return None
    stripped = command.strip()
    for spec in KIND_REGISTRY.values():
        if stripped == spec.cli_subcommand or stripped == spec.name:
            return spec
    return None
