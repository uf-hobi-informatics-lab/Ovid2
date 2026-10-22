"""Semantic handle — the frozen contract every semantic entity satisfies.

Ovid2's *semantics layer* is a kind-keyed registry of small, immutable
descriptors that add meaning on top of the structural :class:`~ovid2.cdm.handle.CdmHandle`.

Phase S1 introduced the first kind — ``vocabulary`` — via
:class:`VocabularyHandle`, which describes a controlled code system such
as OMOP CONCEPT, SNOMED CT, LOINC, ICD-10-CM, RxNorm, or NDC.

Phase C1 widens the vocabulary source legs from two to four so a
vocabulary can additionally live inside a registered Ovid2 address (SQL
or filesystem) or as a small inline enum shipped in the CDM YAML. This
is what makes it possible to store PCORnet valuesets (SEX, DX_TYPE, …)
directly in the ``cdm.yaml`` bundle and to persist larger vocabularies
into a Snowflake reference schema without touching the pipeline
addresses stack.

Design goals:

- **Additive.** The pipeline continues to run unchanged when nothing
  imports the semantic layer. Nothing in ``core/``, ``transformations/``,
  ``formatters/`` or ``mappers/`` reaches into ``ovid2.semantic``.
- **Kind-keyed.** The same registry pattern accepts future kinds
  (metrics, catalog entries) without reworking the plumbing. New kinds
  subclass :class:`SemanticHandle` and register alongside the built-ins.
- **Reuse of existing shared views.** A :class:`VocabularyHandle` may
  point at an already-registered :class:`~ovid2.cdm.handle.SharedViewSpec`
  (e.g. the OMOP CONCEPT shared view that ships with the PCORnet CDM),
  which avoids duplicating I/O plumbing.
- **Storage-agnostic (C1).** A vocabulary may declare an ``address``
  name and be materialised from any registered address type at pipeline
  bootstrap. Small enums may declare ``inline_items`` and be
  materialised driver-side from the YAML itself.

See the adr-semantic-layer design note and
the adr-cdm-yaml-and-route-kinds design note for the
full design rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ovid2.cdm.handle import SharedViewSpec


@dataclass(frozen=True)
class SemanticHandle:
    """Base class for every semantic-layer registration.

    Concrete subclasses declare a class-level :attr:`kind` and add their
    own fields. The registry stores handles keyed by ``(kind, name)``.

    Attributes:
        name: Unique identifier within a kind. Dotted-lowercase by
            convention for cross-CDM vocabularies
            (e.g. ``'omop_concept'``, ``'pcornet_7_0.demographic.sex'``).
        version: Human-readable version tag, informational only.
        description: Free-form summary shown by ``ovid2 semantic show``.
    """

    name: str
    version: str = ""
    description: str = ""

    #: Class-level discriminator. Subclasses override this.
    kind: str = "semantic"


@dataclass(frozen=True)
class VocabularyHandle(SemanticHandle):
    """A controlled clinical vocabulary usable by mappers and adapters.

    A vocabulary is realised at runtime as a Spark temp view named
    :attr:`view_name`, populated from **exactly one** of four source legs:

    - **A filesystem CSV** (``source_path`` is set).
    - **An already-registered shared view** (``shared_view`` is set),
      typically one that the active CDM's ``shared_views`` tuple has
      declared. This is how OMOP CONCEPT is reused.
    - **A registered Ovid2 address** (``address`` is set). Pipeline code
      loads the address via :func:`ovid2.core.loaders.load_address` and
      calls ``read(spark, "__vocab__", "__ref__", table)`` at bootstrap.
      The address may point at any connector — Snowflake, Postgres,
      Databricks, or filesystem — so operators can host reference
      vocabularies wherever they already have infrastructure.
    - **An inline enum** (``inline_items`` is a non-empty tuple). Used
      by small CDM-declared valuesets (SEX=A/F/M/…, DX_TYPE=09/10/…);
      the driver builds a tiny DataFrame with :attr:`code_column` and
      :attr:`description_column` and registers it as a temp view.

    :meth:`__post_init__` enforces exclusivity — exactly one leg may
    be set. It also fills in :attr:`view_name` when omitted.

    Attributes:
        view_name: Spark temp-view name to expose. Defaults to
            :attr:`name` with dots replaced by underscores (so a
            dotted-lowercase vocabulary name like
            ``pcornet_7_0.demographic.sex`` produces the
            SQL-friendly view ``pcornet_7_0_demographic_sex``).
        code_column: Column holding the code value (e.g. ``concept_code``
            for OMOP CONCEPT, ``loinc_num`` for LOINC, ``code`` for
            inline enums).
        description_column: Column holding the human-readable label, if any.
        domain_column: Column carrying a domain / semantic-type discriminator
            (OMOP ``domain_id``, LOINC ``class`` etc.). ``None`` for flat
            vocabularies without one.
        source_path: Filesystem path to a CSV to be registered as
            :attr:`view_name`. Mutually exclusive with the other source
            legs.
        shared_view: An existing :class:`SharedViewSpec` whose view this
            vocabulary reuses. Mutually exclusive with the other source
            legs.
        address: Name of a registered Ovid2 address that resolves to a
            table containing the vocabulary rows. Mutually exclusive
            with the other source legs.
        address_table: Table name to pass to ``address.read`` when
            ``address`` is set. Ignored otherwise.
        inline_items: Tuple of ``(code, label)`` pairs shipped inline
            in the vocabulary YAML. Mutually exclusive with the other
            source legs.
        delimiter: CSV column separator when reading :attr:`source_path`.
        header: Whether :attr:`source_path` has a header row.
        required: When ``True``, missing data raises; when ``False``,
            missing data emits a warning and continues.
        tags: Optional metadata tags — e.g. ``('clinical', 'omop_native')``.
    """

    kind: str = "vocabulary"
    view_name: str = ""
    code_column: str = ""
    description_column: str | None = None
    domain_column: str | None = None
    source_path: Path | None = None
    shared_view: SharedViewSpec | None = None
    address: str | None = None
    address_table: str | None = None
    inline_items: tuple[tuple[str, str | None], ...] = ()
    delimiter: str = "\t"
    header: bool = True
    required: bool = False
    #: Optional metadata tags — e.g. ``('clinical', 'omop_native')``.
    tags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """Validate invariants that json-schema cannot express."""
        # Default the view name to a SQL-safe form of the vocabulary name.
        if not self.view_name:
            object.__setattr__(self, "view_name", self.name.replace(".", "_"))
        legs = [
            ("source_path", self.source_path is not None),
            ("shared_view", self.shared_view is not None),
            ("address", self.address is not None),
            ("inline_items", bool(self.inline_items)),
        ]
        set_legs = [name for name, is_set in legs if is_set]
        if len(set_legs) == 0:
            raise ValueError(
                f"VocabularyHandle '{self.name}' must set exactly one of "
                f"'source_path', 'shared_view', 'address', or 'inline_items'."
            )
        if len(set_legs) > 1:
            raise ValueError(
                f"VocabularyHandle '{self.name}' cannot set multiple source "
                f"legs: {set_legs}."
            )
        if not self.code_column:
            raise ValueError(f"VocabularyHandle '{self.name}' requires a non-empty 'code_column'.")
        if self.address is not None and not self.address_table:
            raise ValueError(
                f"VocabularyHandle '{self.name}': 'address' requires 'address_table'."
            )

    # ------------------------------------------------------------------ #
    # Access helpers                                                     #
    # ------------------------------------------------------------------ #

    def resolved_view_name(self) -> str:
        """Return the temp-view name Spark SQL should query.

        When the vocabulary reuses a :attr:`shared_view`, returns that
        view's ``view_name``. Otherwise returns :attr:`view_name`.
        """
        if self.shared_view is not None:
            return self.shared_view.view_name
        return self.view_name

    def resolved_source_path(self) -> Path | None:
        """Return the filesystem path Spark should read from, if any."""
        if self.shared_view is not None:
            return self.shared_view.source_path
        return self.source_path

    def source_leg(self) -> str:
        """Return which of the four legs this vocabulary uses.

        Values: ``'source_path'``, ``'shared_view'``, ``'address'``,
        ``'inline_items'``.
        """
        if self.source_path is not None:
            return "source_path"
        if self.shared_view is not None:
            return "shared_view"
        if self.address is not None:
            return "address"
        return "inline_items"
