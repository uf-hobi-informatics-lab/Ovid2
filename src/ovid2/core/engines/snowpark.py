"""Snowpark compute engine.

Wraps a :class:`snowflake.snowpark.Session`. Snowpark is **not**
API-compatible with a real ``SparkSession``: DataFrames are a different
type, ``pyspark.sql.functions.*`` does not resolve, and several
Spark-only concepts (``pandas_udf``, ``broadcast()``, ``input_file_name()``)
have no direct analog.

This engine therefore:

1. Advertises ``family = SNOWPARK`` and ``session_is_spark = False``.
2. Sets ``supports_pandas_udf = supports_broadcast_hint =
   supports_input_file_name = supports_local_files = False``.
3. Returns a :class:`SnowparkEngineSession` adapter from
   :meth:`get_session` that speaks the :class:`EngineSession` protocol.
4. Returns a :class:`_SnowparkFunctions` adapter from
   :meth:`get_functions` that maps to :mod:`snowflake.snowpark.functions`
   where possible and raises :class:`EngineOperationUnsupported` for the
   Spark-only names.

Pipelines whose formatters or fixers require Spark family must declare
``requires_family = "spark"`` on their pipeline_settings; the pipeline
consults this before dispatching.

## Credential sourcing

There are two ways to provide account + credentials:

1. **Reference an existing Snowflake connection** — set
   ``args['connection']`` to the name of a W7 ``snowflake`` connection.
   The engine loads it via
   :func:`ovid2.config.connections.load_connection` and calls
   :meth:`SnowflakeConnection.build_connect_kwargs` at session-build
   time. This is the recommended path — it reuses the connection's
   file-based key-pair auth model
   (``/usr/include/rsa/rsa_key.p8`` + ``credentials`` passphrase file)
   and keeps the private-key material off the panel's
   ``secrets.d/`` directory entirely.
2. **Legacy inline** — provide ``account`` + ``user`` in ``args`` and
   one of ``password`` / ``private_key`` in the engine's own
   ``secrets`` payload. This path is unchanged for backward compat
   with existing engine JSONs.

When both are set, ``args['warehouse' | 'database' | 'schema' | 'role']``
override the connection-provided values so a single connection can
serve multiple engines that differ only in warehouse.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from ovid2.core.engines.base import BaseEngine, EngineTestResult
from ovid2.core.engines.protocols import (
    EngineFamily,
    EngineFunctions,
    EngineOperationUnsupported,
    EngineSession,
)

if TYPE_CHECKING:
    from snowflake.snowpark import Session as SnowparkNativeSession

_log = logging.getLogger(__name__)


#: Ordered Java/Spark datetime-pattern token → Snowflake ``to_char`` /
#: ``to_date`` token map. Applied by :func:`_java_to_snowflake_format` so
#: pipeline formatters authored against the Spark ``date_format`` /
#: ``to_date`` pattern dialect (``yyyy-MM-dd HH:mm:ss``) produce the same
#: rendering under Snowpark, whose native dialect differs
#: (``YYYY-MM-DD HH24:MI:SS``). Order matters — longer tokens first so
#: ``yyyy`` is consumed before ``yy`` and ``mm`` (minutes) is not mistaken
#: for ``MM`` (months). We scan the pattern left-to-right, emitting literal
#: runs verbatim and translating only recognised token runs.
_JAVA_TO_SNOWFLAKE_TOKENS: tuple[tuple[str, str], ...] = (
    ("yyyy", "YYYY"),
    ("yy", "YY"),
    ("MM", "MM"),
    ("dd", "DD"),
    ("HH", "HH24"),
    ("hh", "HH12"),
    ("mm", "MI"),
    ("ss", "SS"),
    ("SSS", "FF3"),
)


def _java_to_snowflake_format(fmt: str) -> str:
    """Translate a Java/Spark datetime pattern to a Snowflake format string.

    Handles the token set the pipeline actually uses (``yyyy``, ``MM``,
    ``dd``, ``HH``, ``mm``, ``ss`` and their combinations). Unrecognised
    characters (separators like ``-``, ``:``, spaces, and any literal
    digits such as the ``00:00:00`` in ``yyyy-MM-dd 00:00:00``) pass
    through unchanged.

    Args:
        fmt: A Java/Spark ``SimpleDateFormat``-style pattern.

    Returns:
        The equivalent Snowflake datetime format string.
    """
    out: list[str] = []
    i = 0
    n = len(fmt)
    while i < n:
        matched = False
        for java_token, sf_token in _JAVA_TO_SNOWFLAKE_TOKENS:
            if fmt.startswith(java_token, i):
                out.append(sf_token)
                i += len(java_token)
                matched = True
                break
        if not matched:
            out.append(fmt[i])
            i += 1
    return "".join(out)


class _SnowparkFunctions:
    """Adapter over :mod:`snowflake.snowpark.functions`.

    Snowpark's function set covers most PCORnet-style column expressions
    (``col``, ``lit``, ``when``, ``coalesce``, ``upper``, ``regexp_replace``,
    ``to_date``, ``count``, ...). The Spark-only members raise
    :class:`EngineOperationUnsupported`.
    """

    def __init__(self) -> None:
        from snowflake.snowpark import functions as F

        self._F = F

    def col(self, name: str) -> Any:
        return self._F.col(name)

    def lit(self, value: Any) -> Any:
        return self._F.lit(value)

    def when(self, cond: Any, value: Any) -> Any:
        return self._F.when(cond, value)

    def coalesce(self, *cols: Any) -> Any:
        return self._F.coalesce(*cols)

    def upper(self, c: Any) -> Any:
        return self._F.upper(c)

    def lower(self, c: Any) -> Any:
        return self._F.lower(c)

    def trim(self, c: Any) -> Any:
        return self._F.trim(c)

    def substring(self, c: Any, pos: int, length: int) -> Any:
        return self._F.substring(c, pos, length)

    def regexp_replace(self, c: Any, pattern: str, replacement: str) -> Any:
        return self._F.regexp_replace(c, pattern, replacement)

    def regexp_extract(self, c: Any, pattern: str, idx: int) -> Any:
        # Snowpark's own ``regexp_extract`` mirrors Spark exactly (capture
        # group ``idx``, ``""`` on no match). The previous hand-rolled
        # ``regexp_substr(c, pattern, 1, idx)`` raised ``TypeError`` (ints
        # are not Columns) and put ``idx`` in the *occurrence* slot.
        return self._F.regexp_extract(c, pattern, idx)

    def to_date(self, c: Any, fmt: str | None = None) -> Any:
        # Spark's ``to_date`` returns NULL when the input does not match
        # the format (pipeline formatters rely on this — e.g. the person
        # formatter tries ``yyyy-MM-dd`` then ``yyyy-MM-dd HH:mm:ss`` in a
        # coalesce). Snowflake's ``TO_DATE`` *raises* on mismatch, so we
        # map to ``TRY_TO_DATE`` to preserve the null-on-failure contract.
        # We also cast the input to VARCHAR first: when the source is a
        # native Snowflake DATE/TIMESTAMP column (e.g. a Snowflake input
        # address), ``TO_DATE(<date>, <fmt>)`` errors with "too many
        # arguments ... expected 1, got 2". Casting to string makes the
        # two-argument form valid regardless of the source column's type.
        if fmt is None:
            return self._F.builtin("TRY_TO_DATE")(c.cast("string"))
        return self._F.builtin("TRY_TO_DATE")(
            c.cast("string"), self._F.lit(_java_to_snowflake_format(fmt))
        )

    def to_timestamp(self, c: Any, fmt: str | None = None) -> Any:
        # Same rationale as :meth:`to_date` — ``TRY_TO_TIMESTAMP`` for
        # null-on-failure semantics plus a VARCHAR cast so native
        # timestamp/date inputs don't trip the "too many arguments"
        # single-argument-only path.
        if fmt is None:
            return self._F.builtin("TRY_TO_TIMESTAMP")(c.cast("string"))
        return self._F.builtin("TRY_TO_TIMESTAMP")(
            c.cast("string"), self._F.lit(_java_to_snowflake_format(fmt))
        )

    def date_format(self, c: Any, fmt: str) -> Any:
        return self._F.to_char(c, _java_to_snowflake_format(fmt))

    def datediff(self, end: Any, start: Any) -> Any:
        return self._F.datediff("day", start, end)

    def current_timestamp(self) -> Any:
        return self._F.current_timestamp()

    def count(self, c: Any) -> Any:
        return self._F.count(c)  # noqa: OVID2001 aggregate function reference, not an eager action

    def broadcast(self, df: Any) -> Any:
        # Snowflake plans joins itself; the hint is a no-op but we return
        # the DataFrame unchanged so callers do not have to branch.
        return df

    def input_file_name(self) -> Any:
        raise EngineOperationUnsupported(
            "snowpark",
            "input_file_name",
            "Snowpark has no per-row filename pseudo-column",
        )

    def expr(self, sql_string: str) -> Any:
        return self._F.sql_expr(sql_string)

    # --- Portable extensions (E-portability phase 1) --------------------

    def concat(self, *cols: Any) -> Any:
        return self._F.concat(*cols)

    def concat_ws(self, sep: str, *cols: Any) -> Any:
        # Snowpark's concat_ws takes the separator as the first Column arg.
        return self._F.concat_ws(self._F.lit(sep), *cols)

    def current_date(self) -> Any:
        return self._F.current_date()

    def max(self, c: Any) -> Any:
        return self._F.max(c)  # noqa: OVID2001 aggregate function reference, not an eager action

    def map_from_dict(self, mapping: dict[str, Any], key_col: Any, default: Any = None) -> Any:
        """Emit a chained ``when(...).otherwise(...)`` mapping.

        Portable replacement for the Spark ``create_map(...)[key]``
        indexing idiom (Snowpark ``Column`` has no ``__getitem__`` over a
        map literal). Produces an equivalent CASE expression so pipeline
        code never branches on the engine.

        Args:
            mapping: ``{source_value: mapped_value}``.
            key_col: The ``Column`` whose value is looked up.
            default: Value used when ``key_col`` matches no key.

        Returns:
            A Snowpark ``Column`` expressing the lookup.
        """
        if not mapping:
            return self._F.lit(default)
        items = iter(mapping.items())
        first_key, first_val = next(items)
        expr = self._F.when(key_col == self._F.lit(first_key), self._F.lit(first_val))
        for key, val in items:
            expr = expr.when(key_col == self._F.lit(key), self._F.lit(val))
        return expr.otherwise(self._F.lit(default))

    def stable_hash(self, *cols: Any) -> Any:
        """Return a deterministic non-negative integer as a string.

        Snowflake's ``HASH(...)`` returns a signed 64-bit value; ``ABS``
        of it gives a non-negative integer that we render as a string
        (OMOP surrogate ids are ``RDBMS Number``). Null / empty
        components are coalesced to the empty string so a missing part
        still yields a stable id, matching the PySpark ``stable_hash``
        contract (stable within a single engine).
        """
        parts = [self._F.coalesce(c.cast("string"), self._F.lit("")) for c in cols]
        return self._F.abs(self._F.hash(*parts)).cast("string")

    def rand(self, seed: int | None = None) -> Any:
        # Snowpark's random() takes an optional integer seed.
        if seed is None:
            return self._F.random()
        return self._F.random(seed)

    # --- Portable extensions (health / EDC checks) ----------------------

    def length(self, c: Any) -> Any:
        return self._F.length(c)

    def to_double(self, c: Any) -> Any:
        # ``CAST(x AS DOUBLE)`` raises on a non-numeric string in
        # Snowflake; ``TRY_TO_DOUBLE`` returns NULL — the Spark (ANSI off)
        # contract. ``TRY_TO_DOUBLE`` only accepts text, so cast first
        # (native NUMBER columns become their decimal rendering).
        return self._F.builtin("TRY_TO_DOUBLE")(c.cast("string"))


#: Portable type-name -> ``snowflake.snowpark.types`` class name, consumed
#: by :meth:`SnowparkEngine.create_typed_dataframe`. Mirrors
#: :data:`ovid2.core.engines.pyspark._PORTABLE_SPARK_TYPES`.
_PORTABLE_SNOWPARK_TYPES: dict[str, str] = {
    "string": "StringType",
    "double": "DoubleType",
    "boolean": "BooleanType",
    "long": "LongType",
    "date": "DateType",
    "timestamp": "TimestampType",
}


class SnowparkEngineSession:
    """Adapter that presents a :class:`snowflake.snowpark.Session` as an
    :class:`EngineSession`.

    Only the subset of session methods the pipeline actually uses is
    provided. Callers reaching for Spark-specific escape hatches should
    go through :meth:`SnowparkEngine.native` instead.
    """

    def __init__(self, native: SnowparkNativeSession) -> None:
        self._native = native

    def table(self, name: str) -> Any:
        return self._native.table(name)

    def sql(self, query: str) -> Any:
        return self._native.sql(query)

    def createDataFrame(self, data: Any, schema: Any = None) -> Any:
        # Note: Snowpark's create_dataframe signature is `create_dataframe`.
        return self._native.create_dataframe(data, schema=schema)

    @property
    def native(self) -> SnowparkNativeSession:
        return self._native


class SnowparkEngine(BaseEngine):
    """Concrete :class:`BaseEngine` backed by a Snowpark session."""

    type_name = "snowpark"
    family = EngineFamily.SNOWPARK
    session_is_spark = False
    supports_pandas_udf = False
    supports_broadcast_hint = False
    supports_input_file_name = False
    supports_local_files = False

    def __init__(
        self,
        args: dict[str, Any] | None = None,
        secrets: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(args=args, secrets=secrets)
        self._native: SnowparkNativeSession | None = None
        self._session: SnowparkEngineSession | None = None
        self._functions: EngineFunctions | None = None
        #: Scratch-schema fallback databases remembered from the first
        #: :meth:`prepare_workspace` call (see that method).
        self._workspace_fallbacks: tuple[str, ...] = ()

    def _build_configs(self) -> dict[str, Any]:
        """Assemble the Snowpark session-builder config dict.

        Resolution order:

        1. If ``args['connection']`` is a non-empty string, load that
           W7 ``snowflake`` connection and call its
           :meth:`SnowflakeConnection.build_connect_kwargs` for the
           account + auth material. Then let ``args['warehouse']`` /
           ``args['database']`` / ``args['schema']`` / ``args['role']``
           override any values the connection contributed.
        2. Otherwise, fall back to the legacy inline path: require
           ``args['account']`` + ``args['user']`` and consume the
           engine's own ``secrets['password']`` / ``secrets['private_key']``.

        Raises:
            ValueError: When required fields are missing or the
                referenced connection is not a ``snowflake`` type.
            FileNotFoundError: When the referenced connection points at
                a private-key file that does not exist. The exception
                is allowed to propagate so :meth:`test` can convert it
                to an ``auth`` result and callers get a clear message.
        """
        connection_name = (self.args.get("connection") or "").strip()
        if connection_name:
            cfg = self._configs_from_connection(connection_name)
        else:
            cfg = self._configs_inline()

        # Per-engine overrides (warehouse/database/schema/role). These
        # win over whatever the connection contributed so one connection
        # can back multiple engines that differ only in warehouse etc.
        for optional in ("warehouse", "database", "schema", "role"):
            value = self.args.get(optional)
            if value:
                cfg[optional] = str(value)

        # Session parameters that harden long-running statements against
        # the Python connector's retry-on-timeout behaviour.
        #
        # Without ``ABORT_DETACHED_QUERY = TRUE``, a client-side socket /
        # network timeout during a multi-hour ``CREATE OR REPLACE TABLE
        # ... AS SELECT`` (the pcornet_to_omop measurement CTAS scans
        # multiple TB) makes the connector resubmit the statement while
        # the original keeps running server-side — producing two or more
        # identical CTAS executions that compete for the same warehouse
        # and slow every one of them down. ``CLIENT_SESSION_KEEP_ALIVE``
        # keeps the long statement's connection alive so the timeout that
        # triggers the resubmit never fires.
        #
        # The defaults live in ``ovid2.config.settings`` and can be
        # overridden per-engine via the engine JSON's ``session_parameters``
        # arg (operator values win over the settings defaults).
        from ovid2.config import settings as _settings

        session_params: dict[str, Any] = dict(_settings.SNOWPARK_SESSION_PARAMETERS)
        engine_params = self.args.get("session_parameters")
        if isinstance(engine_params, dict):
            session_params.update(engine_params)
        for key, value in session_params.items():
            # Do not clobber account / auth / warehouse keys a caller may
            # have legitimately spelled as a "session parameter".
            cfg.setdefault(key, value)
        return cfg

    def _configs_from_connection(self, connection_name: str) -> dict[str, Any]:
        """Return session-builder configs sourced from a W7 connection.

        Local import of :mod:`ovid2.config.connections` keeps the
        module-load cost of ``SnowparkEngine`` bounded and avoids a
        top-level import cycle (connections → config → connections).

        Args:
            connection_name: Filename stem of the connection JSON.

        Returns:
            A dict with the keys ``snowflake.snowpark.Session.builder.configs``
            accepts: at minimum ``account`` + ``user`` + one of
            ``password`` / ``private_key``. May also contain
            ``warehouse``, ``role``, ``authenticator``.

        Raises:
            ValueError: When the named connection is not of type
                ``snowflake``.
        """
        from ovid2.config.connections import load_connection

        conn = load_connection(connection_name)
        if getattr(conn, "type_name", None) != "snowflake":
            raise ValueError(
                f"snowpark: connection '{connection_name}' has type "
                f"'{getattr(conn, 'type_name', '?')}', expected 'snowflake'"
            )
        cfg: dict[str, Any] = dict(conn.build_connect_kwargs())
        if not cfg.get("account") or not cfg.get("user"):
            raise ValueError(
                f"snowpark: connection '{connection_name}' did not yield "
                "'account' + 'user' — check the connection JSON."
            )
        if not cfg.get("password") and not cfg.get("private_key"):
            raise ValueError(
                f"snowpark: connection '{connection_name}' did not yield "
                "credentials — provide a password secret or make the "
                "private-key files readable."
            )
        return cfg

    def _configs_inline(self) -> dict[str, Any]:
        """Return session-builder configs from the engine's own args + secrets.

        Legacy path — kept unchanged so existing engine JSONs continue
        to load without edits.

        Raises:
            ValueError: When ``account`` / ``user`` are missing, or
                when neither ``password`` nor ``private_key`` is
                present in ``secrets``.
        """
        required = ("account", "user")
        missing = [k for k in required if not self.args.get(k)]
        if missing:
            raise ValueError(f"snowpark: missing required args {missing} (and no 'connection' set)")
        cfg: dict[str, Any] = {
            "account": str(self.args["account"]),
            "user": str(self.args["user"]),
        }
        password = self.secrets.get("password")
        private_key = self.secrets.get("private_key")
        if password:
            cfg["password"] = str(password)
        elif private_key:
            cfg["private_key"] = private_key
        else:
            raise ValueError(
                "snowpark: one of secrets 'password' or 'private_key' is "
                "required when 'connection' is not set"
            )
        return cfg

    def get_session(self) -> EngineSession:
        """Return the Snowpark session adapter, creating it lazily."""
        if self._session is None:
            from snowflake.snowpark import Session

            configs = self._build_configs()
            self._native = Session.builder.configs(configs).create()
            self._session = SnowparkEngineSession(self._native)
        return self._session

    def stop_session(self) -> None:
        """Close the Snowpark session if one has been created.

        Staging strategies' per-run cleanups (``@~`` stage files,
        transient tables, external stages) run first, over the still-open
        session — after ``close()`` they could only fail.
        """
        if self._native is not None:
            from ovid2.core.engines.stager import run_session_cleanups

            run_session_cleanups(self._native)
            with contextlib.suppress(Exception):
                self._native.close()
            self._native = None
            self._session = None
            self._functions = None

    def cancel_running_queries(self) -> None:
        """Cancel every query running on this session (best-effort).

        Called from the ETL entry point's signal handler when an
        operator cancels a Snowpark run from the web panel. Snowflake
        exposes ``SYSTEM$CANCEL_ALL_QUERIES(<session_id>)`` which aborts
        the statements in flight on a given session without closing the
        session itself. We resolve the session id from the live Snowpark
        ``Session`` (falling back to its underlying connector
        connection) and issue the cancel over the same session.

        Fully defensive: any failure (no session, missing session id,
        Snowflake error, the session already closed) is swallowed and
        logged — the caller is unwinding on a signal and must still
        reach its ``finally`` teardown.
        """
        native = self._native
        if native is None:
            _log.debug("cancel_running_queries: no live Snowpark session")
            return
        session_id = self._resolve_session_id(native)
        if session_id is None:
            _log.warning(
                "cancel_running_queries: could not resolve Snowflake "
                "session id; skipping SYSTEM$CANCEL_ALL_QUERIES"
            )
            return
        try:
            # ``session_id`` is a Snowflake-issued integer; there is no
            # bind-parameter form for a session function, so we build the
            # statement by concatenating the validated int (never
            # operator input) — no f-string SQL, no injection surface.
            cancel_sql = "SELECT SYSTEM$CANCEL_ALL_QUERIES(" + str(int(session_id)) + ")"
            native.sql(cancel_sql).collect()  # noqa: OVID2001
            _log.info(
                "Issued SYSTEM$CANCEL_ALL_QUERIES for Snowflake session %s",
                session_id,
            )
        except Exception as exc:
            _log.warning("SYSTEM$CANCEL_ALL_QUERIES(%s) failed: %s", session_id, exc)

    @staticmethod
    def _resolve_session_id(native: SnowparkNativeSession) -> int | None:
        """Best-effort resolve the numeric Snowflake session id.

        Snowpark's ``Session`` exposes ``session_id``; older builds only
        surface it on the underlying ``snowflake.connector`` connection
        (``session.connection.session_id``). Returns ``None`` when
        neither yields a positive integer.
        """
        candidates = []
        sid = getattr(native, "session_id", None)
        candidates.append(sid)
        conn = getattr(native, "connection", None)
        if conn is not None:
            candidates.append(getattr(conn, "session_id", None))
        for cand in candidates:
            try:
                if cand is None:
                    continue
                value = int(cand)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

    def prepare_workspace(self, fallback_databases: Sequence[str] = ()) -> None:
        """Bind the session to a writable database + schema for temp objects.

        Snowpark temporary views (``create_or_replace_temp_view``) and
        transient staging tables are created *inside* the session's
        current database + schema. When the Snowpark connection sets no
        default ``database`` the session starts unbound and the disc
        staging strategy binds the read-only ``SNOWFLAKE`` system
        database purely so ``@~`` user-stage reads resolve — but temp
        views cannot be created there ("Creating view on shared database
        'SNOWFLAKE' is not allowed").

        This hook resolves a writable ``<db>.<schema>`` (the same
        ``OVID2_SNOWPARK_STAGING`` schema the disc strategy uses, honouring
        ``$OVID2_SNOWPARK_STAGING_SCHEMA``) and issues ``USE DATABASE`` /
        ``USE SCHEMA`` so every subsequent temp view / transient table
        lands in a namespace the operator's role can write to. Operators
        who set the engine's ``database`` field already get a writable
        current database; this hook then simply reuses it.

        When neither the env override nor the session names a writable
        database, ``fallback_databases`` (the run's Snowflake output /
        input address databases, output first) are tried in order. The
        fallbacks are remembered, so the per-table re-bind
        (``core/pipeline.py``) calls with no argument and still resolves
        the same schema.

        Args:
            fallback_databases: Candidate databases for the scratch
                schema, tried in order after the session's own database.
        """
        from ovid2.drivers.snowflake.staging.disc import (
            _ensure_writable_staging_schema,
        )

        if fallback_databases:
            self._workspace_fallbacks = tuple(fallback_databases)

        native = self._native
        if native is None:
            native = self.get_session().native  # type: ignore[attr-defined]

        staging_schema = _ensure_writable_staging_schema(
            native, fallback_databases=self._workspace_fallbacks
        )
        database = staging_schema.partition(".")[0]
        native.sql(f"USE DATABASE {database}").collect()  # noqa: OVID2001 session state
        native.sql(f"USE SCHEMA {staging_schema}").collect()  # noqa: OVID2001 session state

    def create_typed_dataframe(
        self,
        rows: list[tuple[Any, ...]],
        schema: list[tuple[str, str]],
    ) -> Any:
        """Build a Snowpark DataFrame from Python rows + a portable schema.

        See :meth:`BaseEngine.create_typed_dataframe`. Every column is
        nullable.

        Raises:
            ValueError: On an unknown portable type name.
        """
        from snowflake.snowpark import types as T

        fields = []
        for name, type_name in schema:
            cls_name = _PORTABLE_SNOWPARK_TYPES.get(type_name)
            if cls_name is None:
                raise ValueError(f"create_typed_dataframe: unknown portable type {type_name!r}")
            fields.append(T.StructField(name, getattr(T, cls_name)(), True))
        native = self.native()
        return native.create_dataframe(rows, schema=T.StructType(fields))

    def get_functions(self) -> EngineFunctions:
        """Return the cached Snowpark :class:`EngineFunctions` adapter."""
        if self._functions is None:
            self._functions = _SnowparkFunctions()
        return self._functions

    # ---- DataFrame-shape portable helpers (snake_case Snowpark API) ----

    def union_by_name(self, left: Any, right: Any, *, allow_missing_columns: bool = False) -> Any:
        """Union two Snowpark DataFrames by column name.

        Snowpark's ``union_by_name`` requires both sides to share the
        same column set — there is no ``allow_missing_columns`` kwarg.
        When ``allow_missing_columns`` is requested we align the schemas
        first: every column present on one side but not the other is
        added as a ``NULL`` literal, then the two are unioned by name.
        """
        if not allow_missing_columns:
            return left.union_by_name(right)

        left_cols = list(left.columns)
        right_cols = list(right.columns)
        left_set = set(left_cols)
        right_set = set(right_cols)

        from snowflake.snowpark import functions as F

        left_aligned = left
        for name in right_cols:
            if name not in left_set:
                left_aligned = left_aligned.with_column(name, F.lit(None))
        right_aligned = right
        for name in left_cols:
            if name not in right_set:
                right_aligned = right_aligned.with_column(name, F.lit(None))
        return left_aligned.union_by_name(right_aligned)

    def drop_duplicates(self, df: Any, cols: list[str] | None = None) -> Any:
        """Drop duplicate rows, optionally scoped to ``cols`` (Snowpark)."""
        if cols is None:
            return df.drop_duplicates()
        return df.drop_duplicates(cols)

    def with_column(self, df: Any, name: str, col: Any) -> Any:
        """Add / replace a column (Snowpark ``with_column``)."""
        return df.with_column(name, col)

    def checkpoint(self, df: Any, name: str) -> Any:
        """Materialise ``df`` to a transient table and return a fresh handle.

        The Snowpark analogue of Spark's ``.cache()``. The default
        ``BaseEngine.checkpoint`` just marks the DataFrame for caching,
        which is a no-op on Snowpark (there is no in-memory cache) — so a
        multi-source lazy plan would still be re-derived by every
        downstream consumer. Here we instead **execute** the plan once
        into a session-scoped transient table and return
        ``session.table(<transient>)``, so the fan-in is computed a
        single time server-side and each downstream branch scans the
        small materialised table.

        The transient table is created in the writable staging schema
        bound by :meth:`prepare_workspace` (honours
        ``$OVID2_SNOWPARK_STAGING_SCHEMA``). It is named
        ``OVID2_CKPT_<sanitised-name>`` so operator reap queries can find
        it (``SHOW TABLES LIKE 'OVID2_CKPT_%'``); a transient table costs
        no Fail-safe storage and is dropped automatically at session end
        (and can be dropped early via the returned handle's session).

        Args:
            df: The Snowpark DataFrame to materialise.
            name: Logical name; sanitised into the transient-table name.

        Returns:
            A ``session.table(...)`` handle onto the materialised result.
        """
        import re

        native = self.native()
        safe = re.sub(r"[^A-Za-z0-9]+", "_", str(name)).strip("_").upper() or "CKPT"
        table_name = f"OVID2_CKPT_{safe}"
        # ``save_as_table`` executes the plan and writes the transient
        # table into the session's current (staging) schema. ``overwrite``
        # keeps re-runs of the same table idempotent within a session.
        df.write.mode("overwrite").save_as_table(
            table_name,
            table_type="transient",
        )
        return native.table(table_name)

    def native(self) -> SnowparkNativeSession:
        """Return the underlying real Snowpark ``Session``."""
        self.get_session()
        assert self._native is not None  # for type-checkers
        return self._native

    def test(self) -> EngineTestResult:
        """Open a session and run ``SELECT 1``."""
        try:
            from snowflake.snowpark import Session  # noqa: F401
        except ImportError as exc:
            return EngineTestResult(
                ok=False,
                code="unreachable",
                message=f"snowflake-snowpark-python not importable: {exc}",
            )
        try:
            session = self.get_session()
            rows = session.sql("SELECT 1 AS ok").collect()  # noqa: OVID2001
            if rows and rows[0][0] == 1:
                # Prefer the account the session was actually built with
                # (may have come from a referenced connection); fall
                # back to whatever the operator typed on the engine
                # itself.
                account = ""
                if self._native is not None:
                    for attr in ("get_current_account", "getCurrentAccount"):
                        getter = getattr(self._native, attr, None)
                        if callable(getter):
                            try:
                                account = str(getter() or "")
                            except Exception:
                                account = ""
                            break
                if not account:
                    account = str(self.args.get("account") or "")
                return EngineTestResult(
                    ok=True,
                    code="ok",
                    message="Snowpark session established",
                    detail={"account": account},
                )
            return EngineTestResult(
                ok=False,
                code="error",
                message="SELECT 1 returned unexpected result",
                detail={"row": str(rows)},
            )
        except ValueError as exc:
            return EngineTestResult(ok=False, code="auth", message=str(exc))
        except (FileNotFoundError, PermissionError, IsADirectoryError) as exc:
            return EngineTestResult(
                ok=False,
                code="auth",
                message=f"Snowflake credentials file unavailable: {exc}",
            )
        except Exception as exc:
            return EngineTestResult(
                ok=False,
                code="unreachable",
                message=f"Snowpark probe failed: {exc}",
                detail={"exception_type": type(exc).__name__},
            )


__all__ = ["SnowparkEngine", "SnowparkEngineSession"]
