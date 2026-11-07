"""Legacy-Ovid deduplication semantics (``legacy_ovid`` profile).

Legacy ``CommonFuncitons.deduplicate`` keyed on *column positions* of the
``formatted_<t>.csv`` file:

=======================  =====================================
Table                    Key (1-based column positions)
=======================  =====================================
ENROLLMENT               1, 2, 5
DEATH_CAUSE              1-5
PAT_RELATIONSHIP         1, 2
PCORNET_TRIAL            1, 2, 3
FACT_RELATIONSHIP        1-5
GOAL                     1-5   (awk path; the >40M Spark path uses 1-6)
TREATMENTTEAM_NURSE      1-5   (awk path; the >40M Spark path uses 1-6)
PROBLEM_CONTACT          1-6
INTERVENTION_CONTACT     1-7
GOAL_CONTACT             1-7
UNIT                     1-7
TUMOR_DATE               *not deduplicated*
(pi) HASH_TOKEN/DEATH/  whole row
VERITAS_EXPANDED
anything else            1
=======================  =====================================

Below 40M rows (``AWK_PASSBY_TRESHHOLD``) it ran ``awk '!seen[$k...]++'``
on the text (keeps the *first* occurrence in file order — emulated with
``row_number()`` over the formatter frame's write order); above, Spark
``dropDuplicates`` (keeps an arbitrary one). The keys above are the awk
keys; the Spark branch disagreed for GOAL / TREATMENTTEAM_NURSE (1-6).
"First in file order" is only as deterministic as the formatter's own
row order: when that depends on Spark partitioning (multi-file globs,
joins, ``explode``) two legacy runs disagree too, and the parity harness
compares such tables on the key (``--key``, ``key_match``).

The awk path compares the *serialised text*: a null field (written empty)
and an empty-string field (written ``""``) are different keys, which is
exactly how ``dropDuplicates`` treats ``null`` vs ``'""'`` on the
W-normalised frame (:func:`ovid2.legacy.csv_roundtrip.legacy_write_normalise`),
so this module dedups *that* frame.

Per-pipeline overrides in ``pipeline_settings``: ``LEGACY_DEDUP_WHOLE_ROW``
(iterable of table names) and ``LEGACY_DEDUP_SKIP``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyspark.sql import DataFrame

#: Table (lower-case) → 1-based key column positions.
LEGACY_POSITIONAL_KEYS: dict[str, tuple[int, ...]] = {
    "enrollment": (1, 2, 5),
    "death_cause": (1, 2, 3, 4, 5),
    "pat_relationship": (1, 2),
    "pcornet_trial": (1, 2, 3),
    "fact_relationship": (1, 2, 3, 4, 5),
    "goal": (1, 2, 3, 4, 5),
    "treatmentteam_nurse": (1, 2, 3, 4, 5),
    "problem_contact": (1, 2, 3, 4, 5, 6),
    "intervention_contact": (1, 2, 3, 4, 5, 6, 7),
    "goal_contact": (1, 2, 3, 4, 5, 6, 7),
    "unit": (1, 2, 3, 4, 5, 6, 7),
}

#: Tables legacy never deduplicated.
LEGACY_SKIP: frozenset[str] = frozenset({"tumor_date"})

#: Sentinel returned by :func:`legacy_dedup_key` for whole-row dedup.
WHOLE_ROW: tuple[str, ...] = ("*",)


def legacy_dedup_key(
    table: str,
    columns: list[str],
    *,
    whole_row_tables: frozenset[str] = frozenset(),
    skip_tables: frozenset[str] = frozenset(),
) -> tuple[str, ...] | None:
    """Return the legacy dedup key for ``table`` given its column order.

    Args:
        table: Table name (any case).
        columns: The formatted frame's columns, in file order.
        whole_row_tables: Tables deduplicated on the entire row.
        skip_tables: Additional tables never deduplicated.

    Returns:
        Column names, :data:`WHOLE_ROW`, or ``None`` (no dedup).
    """
    name = table.lower()
    if name in LEGACY_SKIP or name in skip_tables:
        return None
    if name in whole_row_tables:
        return WHOLE_ROW
    if not columns:
        return None
    positions = LEGACY_POSITIONAL_KEYS.get(name, (1,))
    return tuple(columns[p - 1] for p in positions if p - 1 < len(columns))


def legacy_deduplicate(df: DataFrame, table: str, pipeline: str | None) -> DataFrame:
    """Deduplicate ``df`` (the *formatter* frame) the way legacy did.

    Returns the ``deduplicated_<t>.csv`` **text** (every cell is the exact
    text in the file, like :func:`legacy_write_normalise` output):

    * awk path (the default — every parity-sized table): the formatter
      file ``W(df)`` deduplicated on its text;
    * tables legacy never deduplicated (``TUMOR_DATE``): legacy read the
      formatter file and wrote it back (``spark_read`` →
      ``write_pyspark_output_file``), i.e. ``W(R(W(df)))``. That second
      boundary matters for empty strings (``""`` → empty field) and for
      values the writer escapes (NUL-quoted text gains backslashes).

    The > 40M-row ``dropDuplicates`` path (R → dedup → W) is not emulated;
    the result differs from the awk path only for values whose W∘R∘W text
    differs from W (quote/escape characters, empty strings).

    Args:
        df: Formatter output frame.
        table: Table name.
        pipeline: Pipeline name (for per-pipeline overrides).

    Returns:
        The deduplicated W-text frame (lazy). Callers must apply the
        reader side (:func:`legacy_read_normalise`) before using values, and
        write it with a raw text writer (never re-``W``).
    """
    from ovid2.legacy.csv_roundtrip import legacy_round_trip, legacy_write_normalise
    from ovid2.legacy.profile import pipeline_setting

    whole_row = frozenset(
        str(t).lower() for t in (pipeline_setting(pipeline, "LEGACY_DEDUP_WHOLE_ROW", ()) or ())
    )
    skip = frozenset(
        str(t).lower() for t in (pipeline_setting(pipeline, "LEGACY_DEDUP_SKIP", ()) or ())
    )
    key = legacy_dedup_key(table, list(df.columns), whole_row_tables=whole_row, skip_tables=skip)
    if key is None:
        return legacy_write_normalise(legacy_round_trip(df))
    from pyspark.sql import Window
    from pyspark.sql import functions as sf

    # awk ``!seen[key]++`` keeps the *first* row in file order.
    # ``monotonically_increasing_id`` on the formatter frame is the row
    # order Spark's single-file writer emits (partition id, then row index
    # within the partition) — exactly the order of ``formatted_<t>.csv``.
    # ``dropDuplicates`` would keep an arbitrary row (demo ``death``: the
    # ``*Death_*`` glob also matches ``Death_Cause_*``; legacy keeps the
    # ``Death_`` rows that come first).
    order = "__legacy_file_order"
    written = legacy_write_normalise(df.withColumn(order, sf.monotonically_increasing_id()))
    written = written.withColumn(order, sf.col(order).cast("long"))
    cols = [c for c in written.columns if c != order]
    keys = cols if key == WHOLE_ROW else list(key)
    first = Window.partitionBy(*[sf.col(f"`{k}`") for k in keys]).orderBy(sf.col(order))
    return (
        written.withColumn("__legacy_rank", sf.row_number().over(first))
        .filter(sf.col("__legacy_rank") == 1)
        .select(*[sf.col(f"`{c}`") for c in cols])
    )
