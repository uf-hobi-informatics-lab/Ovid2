"""OMOP concept-id resolution shared across the clinical formatters.

Reproduces the legacy converter's two-hop resolution (the
``*_source_concept_id_mapping_updater`` + ``*_concept_id_mapping_updater``
+ ``filter_common_table`` chain) directly against the ``concept`` and
``concept_relationship`` shared views, so no intermediate mapping tables
are generated:

1. **source_concept_id** — filter ``concept`` to the OMOP ``domain_id``,
   then match the PCORnet source row on ``(concept_code, code_type)``
   where ``code_type`` is the ``vocabulary_id`` collapsed to the PCORnet
   two-letter abbreviation (``ICD-10-CM`` -> ``10`` etc.). Gives
   ``<prefix>_source_concept_id``.
2. **concept_id** — follow ``concept_relationship`` where
   ``relationship_id = 'Maps to'`` (latest ``valid_start_date`` per
   source concept) to the standard ``concept_id``. Gives
   ``<prefix>_concept_id``.

Scaling
-------

The OMOP vocabulary slices are huge (``CONCEPT.csv`` ~900 MB,
``CONCEPT_RELATIONSHIP.csv`` ~3 GB). The original port ``broadcast()``-ed
the per-domain slice and the ``'Maps to'`` slice, which forced a
multi-GB broadcast that either overflowed the driver or crawled for
hours. This module now mirrors the legacy converter
(``21.condition_concept_id_mapping_updater``): it reduces the source data
to its **distinct** ``(code, code_type)`` pairs first, resolves those
against the vocabulary with plain shuffle joins (**no broadcast hint**),
then joins the small resolved mapping back onto the full row set. The
planner picks the join strategy; skew is handled by Spark's own shuffle
(and by the salted deduplicator upstream).

Engine portability
-------------------

All column expressions go through the injected engine DSL
(``functions`` — an :class:`~ovid2.core.engines.protocols.EngineFunctions`)
rather than ``pyspark.sql.functions`` directly, and union / distinct use
the engine's portable helpers, so the resolution runs unchanged under
PySpark and Snowpark.

Everything is lazy. The ``concept`` / ``concept_relationship`` views are
registered by ``ovid2.core.pipeline.register_shared_tables`` (lowercase
columns).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ovid2_pipelines.pcornet_to_omop import dictionaries as _dicts

if TYPE_CHECKING:
    from ovid2.core.engines.base import BaseEngine
    from ovid2.core.engines.protocols import EngineFunctions, EngineSession


def _code_type_column(functions: "EngineFunctions", vocab_col: str) -> Any:
    """Collapse an OMOP ``vocabulary_id`` to the PCORnet two-letter code type."""
    mapped = functions.map_from_dict(
        _dicts.pcornet_code_type_dict,
        functions.upper(functions.col(vocab_col)),
    )
    return functions.coalesce(mapped, functions.lit("NI"))


def source_concept_lookup(
    session: "EngineSession",
    functions: "EngineFunctions",
    *,
    domain: str,
    prefix: str,
) -> Any:
    """Return a ``(code, code_type, <prefix>_source_concept_id)`` lookup.

    Filters the ``concept`` view to ``domain_id = <domain>`` and keeps
    only valid rows (``invalid_reason`` null), matching the legacy
    ``*_source_concept_id`` updater which filters
    ``invalid_reason IS NULL`` before use.
    """
    col = functions.col
    concept = session.table("concept")
    filtered = concept.filter(col("domain_id") == domain)
    return filtered.select(
        col("concept_code").alias(f"{prefix}__lk_code"),
        col("concept_id").alias(f"{prefix}_source_concept_id"),
        _code_type_column(functions, "vocabulary_id").alias(f"{prefix}__lk_code_type"),
        col("invalid_reason").alias(f"{prefix}_source_invalid_reason"),
    )


def maps_to_lookup(
    session: "EngineSession",
    functions: "EngineFunctions",
    *,
    prefix: str,
) -> Any:
    """Return a ``(<prefix>_source_concept_id -> <prefix>_concept_id)`` lookup.

    Follows ``concept_relationship`` ``'Maps to'`` edges, keeping the row
    with the latest ``valid_start_date`` per source concept (matching the
    legacy ``*_concept_id`` updater's ``groupBy(concept_id_1).agg(max(
    valid_start_date))`` reduction).
    """
    col = functions.col
    rel = session.table("concept_relationship").filter(
        col("relationship_id") == "Maps to"
    )
    latest = (
        rel.groupBy("concept_id_1")
        .agg(functions.max("valid_start_date").alias("__max_vsd"))
        .select(
            col("concept_id_1").alias("__grp_cid1"),
            col("__max_vsd"),
        )
    )
    newest = rel.join(
        latest,
        (rel["concept_id_1"] == latest["__grp_cid1"])
        & (rel["valid_start_date"] == latest["__max_vsd"]),
        "inner",
    ).select(
        rel["concept_id_1"].alias(f"{prefix}_source_concept_id_rel"),
        rel["concept_id_2"].alias(f"{prefix}_concept_id"),
    )
    return newest


def resolve_concepts(
    df: Any,
    session: "EngineSession",
    *,
    functions: "EngineFunctions",
    engine: "BaseEngine",
    domain: str,
    prefix: str,
    code_column: str,
    code_type_column: str,
    require_source_concept: bool = True,
    match_code_type: bool = True,
) -> Any:
    """Attach ``<prefix>_source_concept_id`` and ``<prefix>_concept_id``.

    Args:
        df: The source clinical DataFrame (e.g. CONDITION rows).
        session: Active engine session (for the shared views).
        functions: The engine's column-expression DSL.
        engine: The active compute engine (for portable
            distinct / union helpers).
        domain: OMOP ``domain_id`` to restrict the concept lookup to
            (``Condition`` / ``Measurement`` / ``Drug`` / ``Observation``
            / ``Procedure``).
        prefix: Column-name prefix for the resolved ids.
        code_column: The source code column on ``df`` (e.g. ``dx``).
        code_type_column: The source code-type column on ``df`` (e.g.
            ``dx_type``), already holding the PCORnet two-letter type.
        require_source_concept: When ``True`` (legacy default), rows with
            no valid source concept are dropped (inner join). When
            ``False``, they are kept with null resolved ids (left join).
        match_code_type: When ``True`` (legacy default for the coded
            clinical tables), the source-concept join matches on both
            ``code`` *and* ``code_type``. When ``False``, the join matches
            on ``code`` alone — used by tables that carry no reliable code
            type (e.g. PCORnet ``DEATH_CAUSE``, which the legacy converter
            resolved purely on ``DEATH_CAUSE == condition_code``).

    Returns:
        ``df`` plus two resolved-id columns. The vocabulary joins run
        against a distinct-code reduction of ``df`` (no broadcast hint),
        so the multi-GB slices are never materialised whole; execution
        stays lazy.
    """
    col = functions.col

    # ------------------------------------------------------------------
    # 1) Reduce the source data to its distinct (code[, code_type]) pairs
    #    so the vocabulary join is against a tiny mapping table, not the
    #    full row set. Mirrors the legacy distinct-code mapping-updater.
    #    The distinct pairs are renamed to private ``__mk_*`` columns so
    #    joining the mapping back onto ``df`` (step 4) introduces no
    #    ambiguous same-named columns.
    # ------------------------------------------------------------------
    mk_code = f"{prefix}__mk_code"
    mk_code_type = f"{prefix}__mk_code_type"
    if match_code_type:
        codes = engine.drop_duplicates(
            df.select(
                col(code_column).alias(mk_code),
                col(code_type_column).alias(mk_code_type),
            ),
            [mk_code, mk_code_type],
        )
    else:
        codes = engine.drop_duplicates(
            df.select(col(code_column).alias(mk_code)),
            [mk_code],
        )

    # 2) Resolve the distinct codes → source_concept_id via a plain
    #    shuffle join against the domain-filtered, valid concept slice.
    src = source_concept_lookup(session, functions, domain=domain, prefix=prefix).filter(
        col(f"{prefix}_source_invalid_reason").isNull()
    )
    join_cond = col(mk_code) == col(f"{prefix}__lk_code")
    if match_code_type:
        join_cond = join_cond & (col(mk_code_type) == col(f"{prefix}__lk_code_type"))
    mapping_cols = [col(mk_code)]
    if match_code_type:
        mapping_cols.append(col(mk_code_type))
    code_mapping = codes.join(src, join_cond, "inner").select(
        *mapping_cols,
        col(f"{prefix}_source_concept_id"),
    )

    # 3) Follow 'Maps to' to the standard concept_id — again a plain
    #    shuffle join against the small distinct-code mapping.
    rel = maps_to_lookup(session, functions, prefix=prefix)
    code_mapping = code_mapping.join(
        rel,
        col(f"{prefix}_source_concept_id") == col(f"{prefix}_source_concept_id_rel"),
        "left",
    ).select(
        *mapping_cols,
        col(f"{prefix}_source_concept_id"),
        col(f"{prefix}_concept_id"),
    )

    # 4) Join the resolved mapping back onto the full row set. Inner when
    #    a source concept is required (drops unmappable rows, matching the
    #    legacy filter_common_table); left otherwise. The mapping's join
    #    keys are the private ``__mk_*`` columns, so no source column is
    #    shadowed.
    how = "inner" if require_source_concept else "left"
    back_cond = col(code_column) == col(mk_code)
    if match_code_type:
        back_cond = back_cond & (col(code_type_column) == col(mk_code_type))
    joined = df.join(code_mapping, back_cond, how)
    return joined
