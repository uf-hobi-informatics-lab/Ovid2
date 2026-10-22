"""``SnowparkDiscStrategy`` — stage disc CSVs through the Snowflake user stage.

ovid2: allow-sql-fstring — every SQL statement in this module is
constructed from trusted internal values (instance IDs sanitised by
:mod:`ovid2.core.isolation`, table/schema names built from
:func:`re.sub` outputs, and header-derived column names). None of the
f-string interpolations accept operator- or pipeline-supplied
identifiers directly; the strategy is invoked from
:class:`SnowparkEngineStager` which itself is only reachable by an
operator who has already been vetted by the compat check
(:func:`ovid2.drivers.engine_compat.assert_engine_supports_drivers`).

Registered at module import time via
:func:`ovid2.core.engines.stager.register_snowpark_strategy`. The
snowflake driver package's ``__init__.py`` triggers this import so
the strategy self-registers whenever the snowflake driver is loaded
(which is unconditional during CLI/web startup — see
:func:`ovid2.drivers.legacy_adapter.register_all_legacy_drivers`).

The strategy is **the only file under**
:mod:`ovid2.drivers.snowflake.staging` that participates in the
Snowpark data plane. It intentionally avoids importing
:mod:`pyspark`; the return value of :meth:`materialise` is a
:class:`snowflake.snowpark.DataFrame`, not a Spark ``DataFrame``.
Import-safety is enforced by
``tests/contract/test_no_pyspark_outside_engine.py`` (module scope)
and ``tests/contract/test_driver_metadata_import_safe.py``.

Data flow — disc → Snowpark
--------------------------

1. Resolve the source file on the container-visible disc path
   (:func:`ovid2.drivers.disc.spark.resolve_read_path`, imported
   locally so the module keeps a pyspark-free module scope).
2. Detect the delimiter from the first line
   (:func:`ovid2.drivers.disc.spark.detect_delimiter`).
3. Read the header line locally to build an all-string
   :class:`snowflake.snowpark.types.StructType`. The disc reader's
   ``inferSchema=false`` policy matches this behaviour — every
   column is a string until a downstream cast happens.
4. ``PUT file:///<abs-path> @~/<ingest_prefix>/<instance_id>/<pipeline>/<batch_id>/``
   via :meth:`session.file.put`. Snowpark's ``file.put`` gzips the
   file server-side and uploads with parallelism.
5. Return ``session.read.option('field_delimiter', delim)
   .option('skip_header', 1).option('field_optionally_enclosed_by', '"')
   .schema(...).csv('@~/<ingest_prefix>/<instance_id>/<pipeline>/<batch_id>/<basename>.gz')``.
   The ``.gz`` suffix reflects Snowflake's automatic compression.

Data flow — Snowpark → disc
--------------------------

1. Choose a stage subdirectory
   ``@~/<egest_prefix>/<instance_id>/<pipeline>/<batch_id>/<table>/``.
2. ``df.write.copy_into_location(stage_dir, file_format_type='csv',
   field_delimiter=delim, header=True, overwrite=True, single=<from location.single_file>)``.
3. ``session.sql("GET '<stage_dir>' 'file:///<disc-container-abs-path>/<pipeline>/<batch_id>/'").collect()``.
4. If ``single=True`` succeeded, the resulting file is
   ``<table>_0_0_0.csv[.gz]``; rename it to ``<table>.csv``.

Instance-scoped cleanup
-----------------------

All stage paths under ``@~/<prefix>/<instance_id>/…`` are removed
on process exit via an :mod:`atexit` hook registered at strategy
init time. When
:data:`ovid2.config.settings.SNOWPARK_STAGING_KEEP` is truthy,
the hook is skipped (useful for post-mortem inspection).
"""

from __future__ import annotations

import atexit
import logging
import os
import re
from typing import TYPE_CHECKING, Any

from ovid2.core.engines.stager import (
    SnowparkStagingStrategy,
    register_snowpark_strategy,
)

if TYPE_CHECKING:
    from ovid2.core.engines.protocols import EngineSession
    from ovid2.drivers.base import Driver

logger = logging.getLogger(__name__)


class SnowparkDiscStrategy(SnowparkStagingStrategy):
    """Bridge the ``disc`` driver into a Snowpark session via ``@~``.

    Handles both directions:

    * :meth:`materialise` — PUT the disc CSV into
      ``@~/<ingest_prefix>/<instance_id>/…`` and read it back as a
      Snowpark DataFrame.
    * :meth:`persist` — write the DataFrame to
      ``@~/<egest_prefix>/<instance_id>/…`` via
      :meth:`DataFrameWriter.copy_into_location` and GET it back
      to the disc container path.

    Both directions register a session-scoped atexit cleanup on
    the first call so a crashed run leaves at most one
    ``@~/<prefix>/<instance_id>/`` subtree behind (skipped when
    :data:`~ovid2.config.settings.SNOWPARK_STAGING_KEEP` is truthy).
    """

    driver_type = "disc"

    #: Class-level record of the atexit-registered stage paths so
    #: :meth:`_maybe_schedule_cleanup` stays idempotent across many
    #: ``materialise`` / ``persist`` calls in one run. Keyed by
    #: ``id(session)`` because a Snowpark session isn't hashable.
    _cleanups_scheduled: dict[int, set[str]] = {}

    #: Per-session list of transient tables the strategy has
    #: created during ``materialise``. Keyed by ``id(session)``.
    #: The atexit hook drops these alongside the stage files.
    _temp_tables: dict[int, list[str]] = {}

    # ------------------------------------------------------------------
    # Public data plane (invoked by SnowparkEngineStager)
    # ------------------------------------------------------------------

    def materialise(
        self,
        session: "EngineSession",
        driver: "Driver",
        pipeline: str,
        batch_id: str,
        file_name: str,
        delimiter: str | None,
        table_name: str | None,
    ) -> Any:
        """Stage ``file_name`` from disc into ``@~`` and return a Snowpark DF.

        Args:
            session: The Snowpark session wrapper (real
                :class:`snowflake.snowpark.Session` is on ``.native``).
            driver: A :class:`~ovid2.drivers.disc.driver.DiscDriver`
                instance. The strategy consults its
                ``_merged_location_view()`` for the container-side
                root path.
            pipeline: Pipeline name (used for the read path and stage
                subdirectory).
            batch_id: Batch identifier.
            file_name: Source file basename or disc glob.
            delimiter: CSV field separator. When ``None``, the
                strategy auto-detects from the file's first line.
            table_name: Ignored on read; disc reads use ``file_name``.

        Returns:
            A :class:`snowflake.snowpark.DataFrame` reading from the
            staged file (all columns typed as string).

        Raises:
            ValueError: When the resolved source is empty
                (zero-byte header line).
            FileNotFoundError: When ``file_name`` does not match
                any file under the disc root.
        """
        del table_name  # unused on read side

        # Locally import the disc driver's spark helpers to keep this
        # module pyspark-free at module scope. detect_delimiter /
        # resolve_read_path themselves do not import pyspark — they
        # are pure-Python helpers on strings and paths.
        from ovid2.drivers.disc.spark import detect_delimiter, resolve_read_path

        merged = driver._merged_location_view()  # noqa: SLF001
        root = merged["root"]
        fmt = merged["format"]
        if fmt != "csv":
            raise ValueError(
                f"SnowparkDiscStrategy only supports fmt='csv' "
                f"(got {fmt!r}). Non-CSV disc feeds must run under "
                "--engine pyspark."
            )

        file_path = resolve_read_path(root, pipeline, batch_id, file_name)
        if os.path.isdir(file_path):
            # Disc supports directory reads (Spark loads the whole
            # dir); Snowpark can too via a stage prefix, but the
            # implementation is meaningfully different (PUT every
            # file, read the stage as a prefix). Out of scope for
            # Phase C; a pipeline needing this should file a
            # follow-up.
            raise ValueError(
                f"SnowparkDiscStrategy does not yet support directory "
                f"reads (resolved {file_path!r} is a directory). "
                "Use --engine pyspark for now."
            )

        detected_delim = delimiter or detect_delimiter(file_path)

        # Read header line locally to derive the column list (used
        # below to build the SELECT projection over the stage). The
        # disc reader's ``inferSchema=false`` policy matches this
        # behaviour — every column stays a string until a downstream
        # cast happens.
        columns = _read_header_columns(file_path, detected_delim)

        # Instance-scoped stage subdirectory.
        instance_id = _current_instance_id()
        stage_dir = _ingest_stage_dir(instance_id, pipeline, batch_id)
        self._maybe_schedule_cleanup(session, instance_id)

        # PUT the file. Snowpark's session.file.put returns a list
        # of dict-shaped PutResult rows; we don't need the return
        # value but the call must complete before the read.
        native = _native_session(session)
        put_target = f"@~/{stage_dir}/"
        logger.info(
            "SnowparkDiscStrategy: PUT file://%s → @~/%s/", file_path, stage_dir,
        )
        # Snowpark requires a current database context to resolve
        # stage reads — even for the user stage ``@~``. Some
        # connections don't set a default database (a bare
        # account-level connection is the canonical example), so we ensure the session has one bound before
        # the ``PUT`` + ``read.csv``. The choice of database is
        # arbitrary for ``@~`` purposes; we pick ``SNOWFLAKE``
        # (always readable) as a safe default the account already
        # has access to.
        _ensure_current_database(native)
        native.file.put(
            f"file://{file_path}",
            put_target,
            overwrite=True,
            auto_compress=True,
        )

        # Read the staged file into a Snowpark DataFrame.
        #
        # We deliberately avoid Snowpark's ``session.read.csv('@~/…')``
        # DSL: it creates a session-scoped temporary file format
        # (``SNOWPARK_TEMP_FILE_FORMAT_<random>``) under the current
        # schema, which requires ``CREATE FILE FORMAT`` privilege
        # there. Operators rarely have that on the databases their
        # role can *read* (``SNOWFLAKE`` and read-only source
        # databases like ``CHAR_DEMO``). See the failure mode in the
        # Phase-C ADR § "session state and privileges".
        #
        # Instead, we materialise the stage into a session-scoped
        # transient table via ``COPY INTO … FROM @~/…``, then hand
        # back a DataFrame via ``session.table(temp_table)``. The
        # ``FILE_FORMAT => (TYPE => 'CSV' …)`` clause is legal
        # inside ``COPY INTO`` regardless of schema privileges (the
        # format is compiled into the plan, not persisted).
        #
        # The transient table is dropped on the session's atexit
        # cleanup, alongside the ``REMOVE @~/…`` calls. We name it
        # after the instance + pipeline + batch to keep parallel
        # runs isolated, matching the stage scoping.
        staged_name = os.path.basename(file_path)
        stage_read_target = f"@~/{stage_dir}/{staged_name}"

        # Transient table lives inside the writable staging schema
        # (see :func:`_ensure_writable_staging_schema`).
        staging_schema = _ensure_writable_staging_schema(native)
        temp_table = _temp_table_name(
            instance_id, pipeline, batch_id,
            os.path.splitext(staged_name)[0],
        )
        temp_fqtn = f"{staging_schema}.{temp_table}"

        # Build the transient-table DDL. Column names are quoted but
        # **uppercased**, so the staged columns behave like ordinary
        # Snowflake identifiers: an unquoted ``col("concept_id_1")``
        # (which Snowpark folds to ``CONCEPT_ID_1``) resolves against
        # the quoted ``"CONCEPT_ID_1"`` column. If we instead preserved
        # the CSV header's original casing (the OMOP vocabulary exports
        # ship lowercase headers — ``concept_id_1``, ``domain_id`` …),
        # the columns would be quoted-lowercase and every unquoted
        # ``col(...)`` reference would miss. Snowpark tolerates the
        # mismatch on a *base-table* read (case-insensitive single-match
        # resolution) but not after a ``groupBy().agg()`` — the
        # post-aggregation ``select`` derives column state strictly and
        # raises ``invalid identifier 'CONCEPT_ID_1' … existing quoted
        # column identifiers: ['"concept_id_1"' …]``. Uppercasing at DDL
        # time makes both the base read and the aggregated select
        # resolve consistently. The COPY INTO below loads by *position*
        # (``SKIP_HEADER = 1``), so renaming the columns here does not
        # affect the value mapping. This mirrors the Spark path, which
        # is case-insensitive and lowercases the same headers.
        col_ddl = ", ".join(f'"{c.upper()}" VARCHAR' for c in columns)
        create_sql = (
            f"CREATE OR REPLACE TRANSIENT TABLE {temp_fqtn} ({col_ddl})"
        )
        copy_sql = (
            f"COPY INTO {temp_fqtn} FROM '{stage_read_target}' "
            f"FILE_FORMAT = (TYPE = 'CSV' "
            f"FIELD_DELIMITER = '{_escape_sql(detected_delim)}' "
            f"SKIP_HEADER = 1 FIELD_OPTIONALLY_ENCLOSED_BY = '\"' "
            f"TRIM_SPACE = TRUE COMPRESSION = 'AUTO') "
            f"ON_ERROR = 'ABORT_STATEMENT' FORCE = TRUE"
        )
        logger.info(
            "SnowparkDiscStrategy: CREATE %s + COPY INTO from %s",
            temp_fqtn, stage_read_target,
        )
        native.sql(create_sql).collect()
        native.sql(copy_sql).collect()

        # Schedule the transient table for cleanup along with the
        # stage files.
        self._register_temp_table(session, instance_id, temp_fqtn)

        return native.table(temp_fqtn)

    def persist(
        self,
        session: "EngineSession",
        df: Any,
        driver: "Driver",
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str | None,
    ) -> None:
        """Write ``df`` to a stage, then GET it back to disc.

        Args:
            session: The Snowpark session wrapper.
            df: A Snowpark DataFrame produced by :meth:`materialise`
                (or by a :meth:`session.table` / SQL read).
            driver: A :class:`~ovid2.drivers.disc.driver.DiscDriver`
                pointing at the target disc address. The strategy
                writes under ``{root}/{pipeline}/{batch_id}/``.
            pipeline: Pipeline name.
            batch_id: Batch identifier.
            table_name: File stem — the final artefact is
                ``{table_name}.csv``.
            delimiter: CSV field separator. Defaults to
                :data:`ovid2.config.settings.CSV_DELIMITER` when
                ``None``.

        Raises:
            ValueError: When the disc target's ``format`` is not CSV.
            RuntimeError: When the GET step returns zero files.
        """
        from ovid2.config.settings import CSV_DELIMITER

        merged = driver._merged_location_view()  # noqa: SLF001
        root = merged["root"]
        fmt = merged["format"]
        if fmt != "csv":
            raise ValueError(
                f"SnowparkDiscStrategy only supports fmt='csv' "
                f"(got {fmt!r}). Non-CSV disc targets must run under "
                "--engine pyspark."
            )
        single_file = bool(merged.get("single_file", True))
        delim = delimiter or CSV_DELIMITER

        # Ensure the output directory exists on the container-side
        # disc so the GET step has a place to land.
        output_dir = os.path.join(root, pipeline, batch_id)
        os.makedirs(output_dir, exist_ok=True)

        # Instance-scoped stage subdirectory. One subdirectory per
        # (pipeline, batch_id, table) so a run over many tables does
        # not overwrite intermediate stages before the GETs run.
        instance_id = _current_instance_id()
        stage_dir = _egest_stage_dir(instance_id, pipeline, batch_id, table_name)
        self._maybe_schedule_cleanup(session, instance_id)

        # Snowpark's copy_into_location writes CSV via COPY INTO
        # LOCATION under the hood. ``single=True`` requests one
        # merged file (Snowflake enforces a size cap; large frames
        # will split anyway).
        logger.info(
            "SnowparkDiscStrategy: copy_into_location → @~/%s/", stage_dir,
        )
        # Ensure the session has a current database bound so
        # ``@~`` resolves (see materialise() for the rationale).
        _ensure_current_database(_native_session(session))
        _copy_into_location(
            df,
            stage_dir=stage_dir,
            field_delimiter=delim,
            single_file=single_file,
        )

        # GET the staged files back to the disc target. The trailing
        # slash is required on both ends per the Snowflake docs.
        native = _native_session(session)
        get_target_uri = f"file://{output_dir.rstrip('/')}/"
        get_stage_uri = f"@~/{stage_dir}/"
        logger.info(
            "SnowparkDiscStrategy: GET %s → %s", get_stage_uri, get_target_uri,
        )
        rows = native.sql(
            f"GET '{get_stage_uri}' '{get_target_uri}'"
        ).collect()  # noqa: OVID2001
        if not rows:
            raise RuntimeError(
                f"SnowparkDiscStrategy: GET {get_stage_uri} returned "
                "zero rows — expected at least one downloaded file. "
                "Check the operator has Snowflake permission to GET "
                "from their own @~ stage."
            )

        # Rename the downloaded file(s) to the caller's expected
        # ``{table_name}.csv``. Snowflake produces
        # ``data_0_0_0.csv[.gz]`` for a single-file COPY INTO; when
        # ``single=False`` the pattern is ``data_0_0_<part>.csv[.gz]``.
        downloaded = _sorted_downloaded_files(rows, output_dir)
        if not downloaded:
            raise RuntimeError(
                f"SnowparkDiscStrategy: GET succeeded but no CSV file "
                f"appeared under {output_dir!r}. Rows returned: {rows!r}"
            )
        if single_file:
            _finalise_single_file(downloaded, output_dir, table_name)
        else:
            _finalise_multi_part(downloaded, output_dir, table_name)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _maybe_schedule_cleanup(
        self, session: "EngineSession", instance_id: str,
    ) -> None:
        """Register an atexit hook that removes the instance-scoped stage.

        Skipped when :data:`ovid2.config.settings.SNOWPARK_STAGING_KEEP`
        is truthy. Idempotent per ``(session, instance_id)`` pair —
        many ``materialise`` / ``persist`` calls in one run schedule
        at most one hook.
        """
        from ovid2.config.settings import (
            SNOWPARK_STAGING_EGEST_PREFIX,
            SNOWPARK_STAGING_INGEST_PREFIX,
            SNOWPARK_STAGING_KEEP,
        )

        if SNOWPARK_STAGING_KEEP:
            logger.info(
                "SnowparkDiscStrategy: SNOWPARK_STAGING_KEEP is set; "
                "skipping atexit cleanup for instance %s", instance_id,
            )
            return

        session_key = id(session)
        registered = self._cleanups_scheduled.setdefault(session_key, set())
        if instance_id in registered:
            return
        registered.add(instance_id)

        native = _native_session(session)
        temp_tables_ref = self._temp_tables.setdefault(session_key, [])

        def _cleanup() -> None:
            for prefix in (SNOWPARK_STAGING_INGEST_PREFIX, SNOWPARK_STAGING_EGEST_PREFIX):
                stage = f"@~/{prefix}/{instance_id}/"
                try:
                    native.sql(f"REMOVE '{stage}'").collect()  # noqa: OVID2001
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "SnowparkDiscStrategy: REMOVE %s failed (%s); "
                        "stage files may remain — clean manually with "
                        "REMOVE @~/%s/%s/ in a Snowsight worksheet.",
                        stage, exc, prefix, instance_id,
                    )
            for fqtn in temp_tables_ref:
                try:
                    native.sql(f"DROP TABLE IF EXISTS {fqtn}").collect()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "SnowparkDiscStrategy: DROP TABLE %s failed (%s); "
                        "clean manually.", fqtn, exc,
                    )

        atexit.register(_cleanup)

    def _register_temp_table(
        self, session: "EngineSession", instance_id: str, fqtn: str,
    ) -> None:
        """Record ``fqtn`` for atexit cleanup on this session.

        Ensures the atexit hook is scheduled first (so a
        ``_temp_tables`` entry never orphans without a matching
        cleanup). The atexit hook reads the same
        ``_temp_tables[session_key]`` list, so appending after the
        hook has been registered is safe.
        """
        self._maybe_schedule_cleanup(session, instance_id)
        self._temp_tables.setdefault(id(session), []).append(fqtn)


# ---------------------------------------------------------------------------
# Module-level helpers (pyspark-free)
# ---------------------------------------------------------------------------


def _current_instance_id() -> str:
    """Return the process-wide instance id.

    Reads ``$OVID2_INSTANCE_ID`` when set (matches
    :class:`ovid2.core.isolation.OvidInstance`), otherwise falls
    back to a fresh generator so stand-alone invocations still get
    a unique stage subdirectory.
    """
    from ovid2.core.isolation import (
        _generate_instance_id,
        _instance_id_from_env,
    )

    return _instance_id_from_env() or _generate_instance_id()


def _ingest_stage_dir(instance_id: str, pipeline: str, batch_id: str) -> str:
    """Return the ``@~``-relative directory for an ingest staging step.

    Instance scoping ensures parallel runs never collide.
    """
    from ovid2.config.settings import SNOWPARK_STAGING_INGEST_PREFIX

    return f"{SNOWPARK_STAGING_INGEST_PREFIX}/{instance_id}/{pipeline}/{batch_id}"


def _egest_stage_dir(
    instance_id: str, pipeline: str, batch_id: str, table_name: str,
) -> str:
    """Return the ``@~``-relative directory for an egest staging step."""
    from ovid2.config.settings import SNOWPARK_STAGING_EGEST_PREFIX

    return f"{SNOWPARK_STAGING_EGEST_PREFIX}/{instance_id}/{pipeline}/{batch_id}/{table_name}"


def _escape_sql(value: str) -> str:
    """Escape a string literal for inline SQL.

    Doubles single quotes and backslashes. Used to interpolate
    delimiters (``\\t``, ``,``, ``|``) into the ``FIELD_DELIMITER =>
    '...'`` clause of a stage read. Not a general-purpose SQL
    injection guard — the callers pass detected-from-file
    delimiters, not user input.
    """
    return value.replace("\\", "\\\\").replace("'", "''")


def _native_session(session: "EngineSession") -> Any:
    """Return the real ``snowflake.snowpark.Session`` from an adapter."""
    native = getattr(session, "native", None)
    if native is None:
        # Some tests pass a bare session-like double directly.
        return session
    return native


#: Default database used when a Snowpark session has no current
#: database set. ``SNOWFLAKE`` is Snowflake's built-in
#: information-schema database — every account has it and every
#: role can ``USE`` it. We only need *a* database context for the
#: ``@~`` stage plan resolution; we do not read from it.
_DEFAULT_STAGE_DATABASE: str = "SNOWFLAKE"

#: Default schema under :data:`_DEFAULT_STAGE_DATABASE`.
#: ``INFORMATION_SCHEMA`` always exists on ``SNOWFLAKE`` and every
#: user role can ``USE`` it. Only used when the session has no
#: schema set; same rationale as :data:`_DEFAULT_STAGE_DATABASE`.
_DEFAULT_STAGE_SCHEMA: str = "INFORMATION_SCHEMA"

#: Databases that must never be used as a ``CREATE SCHEMA`` target.
#: ``SNOWFLAKE`` is the account-shared system database (Snowflake
#: forbids ``CREATE SCHEMA`` on it — "Creating schema on shared
#: database 'SNOWFLAKE' is not allowed"), and it is also the
#: :data:`_DEFAULT_STAGE_DATABASE` fallback :func:`_ensure_current_database`
#: injects purely to make ``@~`` stage resolution work. Adopting it
#: as the staging database would always fail, so
#: :func:`_ensure_writable_staging_schema` treats it (and the sample
#: database) as "no writable database bound" and raises an actionable
#: error instead of the opaque privilege error. Compared
#: case-insensitively after stripping quotes.
_NON_WRITABLE_SYSTEM_DATABASES: frozenset[str] = frozenset(
    {"SNOWFLAKE", "SNOWFLAKE_SAMPLE_DATA"}
)


def _ensure_current_database(native: Any) -> None:
    """Bind a current database + schema to the session if missing.

    Snowpark's ``read.csv('@~/...')`` and
    ``DataFrameWriter.copy_into_location('@~/...')`` require both
    a current database *and* a current schema — the plan analyzer
    emits ``get_fully_qualified_name_if_possible`` which fails
    when either is missing (``SnowparkMissingDbOrSchemaException``).

    Connections that already set ``database`` + ``schema`` on the
    session builder short-circuit both branches. Idempotent —
    subsequent calls are cheap SQL round-trips.

    Args:
        native: The real :class:`snowflake.snowpark.Session`.
    """
    try:
        current_db = native.get_current_database()
    except Exception:  # noqa: BLE001
        current_db = None
    if not current_db:
        try:
            native.sql(f"USE DATABASE {_DEFAULT_STAGE_DATABASE}").collect()
            logger.info(
                "SnowparkDiscStrategy: bound session to database %s "
                "for @~ resolution.", _DEFAULT_STAGE_DATABASE,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "SnowparkDiscStrategy: could not USE DATABASE %s (%s); "
                "@~ stage reads may fail with 'DATABASE not set'.",
                _DEFAULT_STAGE_DATABASE, exc,
            )
            return

    try:
        current_schema = native.get_current_schema()
    except Exception:  # noqa: BLE001
        current_schema = None
    if current_schema:
        return
    try:
        native.sql(f"USE SCHEMA {_DEFAULT_STAGE_SCHEMA}").collect()
        logger.info(
            "SnowparkDiscStrategy: bound session to schema %s "
            "for @~ resolution.", _DEFAULT_STAGE_SCHEMA,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "SnowparkDiscStrategy: could not USE SCHEMA %s (%s); "
            "@~ stage reads may fail with 'SCHEMA not set'.",
            _DEFAULT_STAGE_SCHEMA, exc,
        )


def _writable_current_database(current_db: Any) -> str | None:
    """Return a usable ``CREATE SCHEMA`` database name, or ``None``.

    Normalises Snowflake's ``get_current_database()`` return value
    (which may be ``None``, a bare name, or a double-quoted name) and
    rejects the account-shared system databases in
    :data:`_NON_WRITABLE_SYSTEM_DATABASES`. The ``SNOWFLAKE`` system
    database in particular is the fallback :func:`_ensure_current_database`
    injects purely for ``@~`` resolution — adopting it as a staging
    database would always fail with "Creating schema on shared
    database 'SNOWFLAKE' is not allowed".

    Args:
        current_db: The raw ``get_current_database()`` result.

    Returns:
        The stripped database name when it is usable, else ``None``.
    """
    if not current_db:
        return None
    name = str(current_db).strip().strip('"')
    if not name:
        return None
    if name.upper() in _NON_WRITABLE_SYSTEM_DATABASES:
        return None
    return name


def _ensure_writable_staging_schema(native: Any) -> str:
    """Return a fully-qualified ``<db>.<schema>`` we can CREATE TABLE in.

    The strategy stages disc data through a transient table (see
    :meth:`SnowparkDiscStrategy.materialise`). Snowpark's
    account-scoped stage ``@~`` has no schema of its own, so the
    transient table lands somewhere else — this helper chooses
    where.

    Resolution order:

    1. ``$OVID2_SNOWPARK_STAGING_SCHEMA`` — operator override.
       Interpreted as either ``<db>.<schema>`` (used verbatim after
       an existence probe) or a bare ``<schema>`` (paired with the
       session's current *writable* database).
    2. ``<current_database>.OVID2_SNOWPARK_STAGING`` — created on
       demand. Requires ``CREATE SCHEMA`` on the current database.
       Idempotent (``CREATE SCHEMA IF NOT EXISTS``); the schema
       persists across runs so parallel invocations don't race on
       creation.

    The account-shared system databases in
    :data:`_NON_WRITABLE_SYSTEM_DATABASES` (notably ``SNOWFLAKE`` — the
    fallback :func:`_ensure_current_database` injects for ``@~``
    resolution) are treated as "no writable database bound": both
    paths reject them via :func:`_writable_current_database` and raise
    an actionable error rather than attempting a doomed
    ``CREATE SCHEMA`` on a read-only shared database.

    Args:
        native: The real Snowpark session.

    Returns:
        A fully-qualified ``<db>.<schema>`` string safe to use
        as the prefix of a ``CREATE TRANSIENT TABLE`` statement.

    Raises:
        RuntimeError: When no writable schema can be resolved. The
            message names the resolution paths so the operator can
            pick one.
    """
    override = os.environ.get("OVID2_SNOWPARK_STAGING_SCHEMA", "").strip()
    if override:
        if "." in override:
            return override
        try:
            current_db = native.get_current_database()
        except Exception:  # noqa: BLE001
            current_db = None
        current_db = _writable_current_database(current_db)
        if not current_db:
            raise RuntimeError(
                "OVID2_SNOWPARK_STAGING_SCHEMA is set to a bare schema "
                f"({override!r}) but the session has no writable current "
                "database (it is unset or resolves to the shared SNOWFLAKE "
                "system database). Set the env var to '<db>.<schema>', or "
                "configure the Snowpark engine's `database` field."
            )
        return f"{current_db}.{override}"

    try:
        current_db = native.get_current_database()
    except Exception:  # noqa: BLE001
        current_db = None
    current_db = _writable_current_database(current_db)
    if not current_db:
        raise RuntimeError(
            "SnowparkDiscStrategy: cannot resolve a writable staging "
            "schema — the session has no writable current database "
            "(it is unset or resolves to the shared read-only SNOWFLAKE "
            "system database, where CREATE SCHEMA is forbidden). Set the "
            "Snowpark engine's `database` field to a database your role "
            "can create schemas in (e.g. the same database your output "
            "address writes to), or export "
            "OVID2_SNOWPARK_STAGING_SCHEMA=<db>.<schema>."
        )
    schema_fqn = f"{current_db}.OVID2_SNOWPARK_STAGING"
    try:
        native.sql(f"CREATE SCHEMA IF NOT EXISTS {schema_fqn}").collect()
        logger.info(
            "SnowparkDiscStrategy: using staging schema %s", schema_fqn,
        )
        return schema_fqn
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"SnowparkDiscStrategy: cannot create staging schema "
            f"{schema_fqn} ({exc}). Grant CREATE SCHEMA on {current_db}, "
            "or export OVID2_SNOWPARK_STAGING_SCHEMA=<existing_writable_schema>."
        ) from exc


def _temp_table_name(
    instance_id: str, pipeline: str, batch_id: str, stem: str,
) -> str:
    """Return a schema-scoped transient table name for a staged CSV.

    Format: ``OVID2_STG_<instance>_<pipeline>_<batch>_<stem>``. Instance
    scoping keeps parallel runs isolated; the ``OVID2_STG_`` prefix
    lets operators reap orphaned tables with a single ``SHOW TABLES
    LIKE 'OVID2_STG_%'`` on the staging schema.
    """
    # Snowflake identifiers uppercase and strip non-[A-Za-z0-9_]
    safe = re.sub(r"[^A-Za-z0-9]+", "_", f"{instance_id}_{pipeline}_{batch_id}_{stem}")
    return f"OVID2_STG_{safe}".upper()


def _read_header_columns(file_path: str, delimiter: str) -> list[str]:
    """Read the first line of ``file_path`` and split on ``delimiter``.

    Returns the split header, trimmed and quote-stripped. Raises
    :class:`ValueError` when the file's first line is empty.
    """
    with open(file_path, encoding="utf-8", errors="replace") as fh:
        first = fh.readline()
    if not first:
        raise ValueError(
            f"SnowparkDiscStrategy: {file_path!r} is empty; nothing to stage."
        )
    columns: list[str] = []
    for raw in first.rstrip("\r\n").split(delimiter):
        col = raw.strip().strip('"')
        columns.append(col or f"_c{len(columns)}")
    if not columns:
        raise ValueError(
            f"SnowparkDiscStrategy: could not extract columns from "
            f"{file_path!r} (delimiter={delimiter!r})."
        )
    return columns


def _build_all_string_schema(columns: list[str]) -> Any:
    """Build an all-string Snowpark :class:`StructType`.

    Local import so the module stays pyspark-free at module scope
    and does not require Snowpark at import time (contract test
    ``test_no_pyspark_outside_engine.py``).
    """
    from snowflake.snowpark.types import StringType, StructField, StructType

    return StructType(
        [StructField(col, StringType(), nullable=True) for col in columns]
    )


def _copy_into_location(
    df: Any,
    *,
    stage_dir: str,
    field_delimiter: str,
    single_file: bool,
) -> None:
    """Run ``df.write.copy_into_location`` with the right options.

    Isolated in a helper so unit tests can stub it out and so the
    keyword-argument shape stays in one place. Snowpark's
    :meth:`DataFrameWriter.copy_into_location` accepts a
    ``format_type_options`` dict for the CSV-specific knobs:
    ``FIELD_DELIMITER``, ``COMPRESSION``, ``NULL_IF``, etc.
    """
    df.write.copy_into_location(
        f"@~/{stage_dir}/",
        file_format_type="csv",
        format_type_options={
            "FIELD_DELIMITER": field_delimiter,
            "COMPRESSION": "NONE",
            "FIELD_OPTIONALLY_ENCLOSED_BY": '"',
            "NULL_IF": ("",),
        },
        header=True,
        overwrite=True,
        single=single_file,
    )


def _sorted_downloaded_files(rows: Any, output_dir: str) -> list[str]:
    """Extract the downloaded-file paths from a ``GET`` result set.

    Snowflake's ``GET`` returns rows with columns ``(file, size,
    status, message)`` where ``file`` is the local basename. We
    filter to files whose status looks OK (``DOWNLOADED`` or
    empty; Snowflake's exact status vocabulary has changed across
    versions), decompress ``.gz`` when needed, then return the
    sorted absolute paths.
    """
    import gzip
    import shutil

    downloaded: list[str] = []
    for row in rows:
        # Snowpark Row objects support both integer and attribute
        # access. Be defensive.
        name = None
        status = ""
        for attr in ("file", "FILE"):
            if hasattr(row, attr):
                name = getattr(row, attr)
                break
        if name is None:
            try:
                name = row[0]
            except (KeyError, IndexError, TypeError):
                continue
        for attr in ("status", "STATUS"):
            if hasattr(row, attr):
                status = str(getattr(row, attr) or "")
                break
        if status and status.upper() not in ("DOWNLOADED", "OK", "SUCCESS"):
            logger.warning(
                "SnowparkDiscStrategy: GET row status=%r for file=%r; "
                "skipping.", status, name,
            )
            continue
        abs_path = os.path.join(output_dir, os.path.basename(str(name)))
        if not os.path.exists(abs_path):
            logger.warning(
                "SnowparkDiscStrategy: expected downloaded file %r not "
                "on disk after GET.", abs_path,
            )
            continue
        if abs_path.endswith(".gz"):
            plain = abs_path[:-3]
            with gzip.open(abs_path, "rb") as src, open(plain, "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.remove(abs_path)
            abs_path = plain
        downloaded.append(abs_path)
    downloaded.sort()
    return downloaded


def _finalise_single_file(
    downloaded: list[str], output_dir: str, table_name: str,
) -> None:
    """Rename a single downloaded CSV to ``{table_name}.csv``.

    When Snowflake's ``single=True`` produced one file, this is a
    plain rename. When it produced more (large frames exceed the
    single-file size cap), we concatenate them locally — the
    stager acts as the single-file finalizer to preserve the disc
    driver's contract.
    """
    dest = os.path.join(output_dir, f"{table_name}.csv")
    if os.path.exists(dest):
        os.remove(dest)
    if len(downloaded) == 1:
        os.rename(downloaded[0], dest)
        return
    # Snowflake exceeded single-file size cap. Concatenate.
    with open(dest, "wb") as out:
        for i, src in enumerate(downloaded):
            with open(src, "rb") as fh:
                if i == 0:
                    out.write(fh.read())
                else:
                    # Skip header on subsequent parts.
                    fh.readline()
                    out.write(fh.read())
            os.remove(src)


def _finalise_multi_part(
    downloaded: list[str], output_dir: str, table_name: str,
) -> None:
    """Rename multi-part downloads to ``{table_name}.part-<N>.csv``.

    Matches the multi-part naming disc's own ``single_file=False``
    path produces (part-directory layout with headers on each part).
    """
    for i, src in enumerate(downloaded):
        dest = os.path.join(output_dir, f"{table_name}.part-{i:05d}.csv")
        if os.path.exists(dest):
            os.remove(dest)
        os.rename(src, dest)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

# Self-register at module import. The snowflake driver's package
# ``__init__.py`` imports this module so it runs during CLI/web
# startup. Idempotent — re-registering the same driver_type
# overwrites the previous entry (per
# :func:`ovid2.core.engines.stager.register_snowpark_strategy`).
register_snowpark_strategy(SnowparkDiscStrategy)


__all__ = ["SnowparkDiscStrategy"]
