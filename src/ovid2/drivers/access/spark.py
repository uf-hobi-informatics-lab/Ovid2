"""Spark-backed read + JDBC table enumeration for the Access driver.

The **only** module under :mod:`ovid2.drivers.access` allowed to
import :mod:`pyspark`. Everything else in the package stays web-safe.
Enforced by ``tests/contract/test_no_pyspark_outside_engine.py`` (the
``drivers/<type>/spark.py`` path is auto-allowed).

Reads use the UCanAccess JDBC driver (baked into the ETL image's
``$SPARK_HOME/jars/``). A single-file Access database has no
natural partition column, so each table is read as one JDBC
partition — acceptable for a raw copy job. UCanAccess is pointed at
the container-visible path of the mounted ``.accdb`` file.

Table enumeration goes through the JDBC ``DatabaseMetaData`` exposed
by py4j on the Spark JVM, so it needs no extra Python dependency and
reuses the exact same driver jars the read path uses.
"""

from __future__ import annotations

import os
import re
import tempfile
from typing import TYPE_CHECKING

from ovid2.drivers.access.reader import (
    UCANACCESS_DRIVER as _UCANACCESS_DRIVER,
)

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, DataFrameReader, SparkSession

#: A "safe" table name that needs no quoting in the ``dbtable`` option.
#: UCanAccess maps the Access file onto HSQLDB with double-quoted
#: identifiers disabled (``"x"`` is parsed as a *string literal*, not an
#: identifier), so a double-quoted ``dbtable`` fails with "unexpected
#: token". Bare identifiers work; names with spaces / punctuation are
#: wrapped in Access-style square brackets, which UCanAccess accepts.
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Fully-qualified class name of the UCanAccess Spark ``JdbcDialect``
#: compiled at Docker build time (``docker/ucanaccess-dialect/`` →
#: ``$SPARK_HOME/jars/ucanaccess-spark-dialect.jar``). It
#: bracket-quotes column identifiers so Spark's JDBC reader stops
#: emitting double-quoted columns (which UCanAccess reads as string
#: literals — every row comes back as its own column names). Registered
#: lazily on the driver JVM by :func:`_register_dialect`.
_DIALECT_CLASS = "net.sf.ucanaccess.spark.UcanaccessDialect"


#: Directory the ETL image bakes ``chmod 777`` on *every* container
#: (master, worker, driver) — see ``docker/Dockerfile`` (``mkdir -p
#: /docker_data/spark_tmp`` + ``chmod -R 777``). It is also ``HOME`` for
#: the image's uid 1001. Because it is created at build time it already
#: exists on the executor (worker) JVMs, which is exactly where
#: UCanAccess opens the connection and calls ``File.createNewFile()`` on
#: the mirror path.
_IMAGE_BAKED_MIRROR_DIR = "/docker_data/spark_tmp"


def _mirror_folder() -> str:
    """Return a writable directory for UCanAccess's HSQLDB mirror files.

    UCanAccess (with ``memory=false``) materialises an on-disk HSQLDB
    copy of the Access database. By default it writes that copy *next
    to* the ``.accdb`` file — which fails when the source lives on a
    read-only bind-mount (the Ovid2 default for an Access connection)
    or in a directory the ETL uid cannot write. Pointing
    ``mirrorFolder`` at a writable directory keeps the source untouched
    and works regardless of the source directory's permissions.

    **Executor vs driver.** In ``cluster``/``client`` mode the per-table
    JDBC read runs on the Spark *executor* (worker container), not the
    driver. The ``mirrorFolder`` value is baked into the JDBC URL string
    *on the driver* (when the DataFrame plan is built), but the actual
    ``UcanaccessDriver.connect()`` → ``File.createNewFile(mirrorFolder…)``
    happens in the *executor* JVM. UCanAccess does **not** ``mkdir -p``
    the mirror folder — it expects the directory to already exist — so a
    driver-side :func:`os.makedirs` is useless for the executor: the
    target must exist on the worker container's own filesystem. ``/tmp``
    is per-container, which is why the previous ``/tmp/ovid2_access_mirror``
    default failed on every executor with ``UCAExc … No such file or
    directory``.

    Resolution order:

    1. ``$OVID2_ACCESS_MIRROR_DIR`` — operator override (created here so
       a bespoke scratch volume works). Assumed present on the executors
       when set intentionally.
    2. :data:`_IMAGE_BAKED_MIRROR_DIR` (``/docker_data/spark_tmp``) — the
       ETL image bakes this ``chmod 777`` on every container, so it is
       guaranteed to exist and be writable on the executor JVMs without
       any runtime ``mkdir``.
    3. ``{tempfile.gettempdir()}/ovid2_access_mirror`` — fallback for
       non-image contexts (unit tests, plain-``python`` local runs),
       created on demand.
    """
    override = os.environ.get("OVID2_ACCESS_MIRROR_DIR")
    if override:
        os.makedirs(override, exist_ok=True)
        return override
    # Image path: exists + world-writable on every container (build-time),
    # so it works on the executor where the connection is actually opened.
    if os.path.isdir(_IMAGE_BAKED_MIRROR_DIR):
        return _IMAGE_BAKED_MIRROR_DIR
    # Non-image fallback (tests / plain-python): create a scratch subdir.
    base = os.path.join(tempfile.gettempdir(), "ovid2_access_mirror")
    os.makedirs(base, exist_ok=True)
    return base


def _jdbc_url(container_file_path: str) -> str:
    """Return the UCanAccess JDBC URL for a mounted Access file.

    ``memory=false`` keeps UCanAccess from loading the whole database
    into the JVM heap — essential for large files (an ~800 MB reference feed
    is ~810 MB) — spilling the HSQLDB mirror to disk instead.
    ``skipIndexes=true`` avoids rebuilding every Access index on open,
    which is pure overhead for a read-only copy. ``mirrorFolder``
    redirects the HSQLDB mirror to a writable temp dir so the source
    ``.accdb`` (typically on a read-only mount) is never written to.
    """
    mirror = _mirror_folder()
    return (
        f"jdbc:ucanaccess://{container_file_path}"
        ";memory=false;skipIndexes=true;openExclusive=false"
        f";mirrorFolder={mirror}"
    )


def is_system_table(name: str, schema: str | None = None) -> bool:
    """Deprecated shim — re-exported from :mod:`ovid2.drivers.access.reader`.

    Kept so existing callers of ``spark.is_system_table`` keep working;
    the canonical implementation now lives in the Spark-free
    :mod:`ovid2.drivers.access.reader` so the web tier shares one policy.
    """
    from ovid2.drivers.access.reader import is_system_table as _canonical

    return _canonical(name, schema)


def get_active_session() -> SparkSession:
    """Return the active Spark session for JVM-backed table browse.

    Kept here (not in the driver) so the pyspark import stays confined
    to this module. Raises :class:`NotImplementedError` when no session
    exists so the web-tier browser stops at the file level rather than
    erroring hard.
    """
    from pyspark.sql import SparkSession

    session = SparkSession.getActiveSession()
    if session is None:
        raise NotImplementedError(
            "listing Access tables requires an active Spark session; "
            "browse stops at the file level in the web tier."
        )
    return session


def list_tables(spark: SparkSession, container_file_path: str) -> list[str]:
    """Enumerate the user tables inside an Access file.

    Opens the file over JDBC on the Spark JVM (via py4j), reads
    ``DatabaseMetaData.getTables(..., ["TABLE"])``, filters out engine
    internals, and returns the user table names sorted
    case-insensitively.

    Args:
        spark: Active Spark session (its JVM gateway is used).
        container_file_path: Container-visible path to the ``.accdb`` /
            ``.mdb`` file.

    Returns:
        Sorted list of user-table names.

    Raises:
        RuntimeError: When the JDBC driver cannot open the file.
    """
    jvm = spark.sparkContext._jvm
    gateway = spark.sparkContext._gateway
    if jvm is None or gateway is None:  # pragma: no cover - JVM always up
        raise RuntimeError(
            "No active JVM gateway on the Spark context; cannot open the Access file over JDBC."
        )
    # Ensure the driver class is loaded. Resolve via the thread context
    # classloader — the system classloader used by a bare
    # ``jvm.Class.forName`` cannot see jars added through ``spark.jars`` /
    # ``--jars`` (only the baked-into-image classpath), so a host run with
    # ``--jars`` would fail with ClassNotFoundException. Connect through
    # the resolved Driver instance directly rather than relying on
    # ``DriverManager`` autoloading (which also consults only the system
    # classloader).
    context_cl = jvm.java.lang.Thread.currentThread().getContextClassLoader()
    driver_cls = jvm.java.lang.Class.forName(_UCANACCESS_DRIVER, True, context_cl)
    driver_instance = driver_cls.newInstance()
    connection = None
    tables: list[str] = []
    try:
        connection = driver_instance.connect(
            _jdbc_url(container_file_path), jvm.java.util.Properties()
        )
        metadata = connection.getMetaData()
        # types=["TABLE"] excludes VIEW / SYSTEM TABLE at the JDBC layer.
        type_array = gateway.new_array(jvm.java.lang.String, 1)
        type_array[0] = "TABLE"
        result_set = metadata.getTables(None, None, "%", type_array)
        try:
            while result_set.next():
                schema = result_set.getString("TABLE_SCHEM")
                name = result_set.getString("TABLE_NAME")
                if name and not is_system_table(name, schema):
                    tables.append(name)
        finally:
            result_set.close()
    except Exception as exc:
        raise RuntimeError(
            f"Failed to enumerate Access tables in {container_file_path!r}: {exc}"
        ) from exc
    finally:
        if connection is not None:
            connection.close()
    return sorted(tables, key=str.lower)


def _quote_identifier(name: str) -> str:
    """Return ``name`` in a form UCanAccess accepts as a table identifier.

    UCanAccess overlays the Access file on HSQLDB with double-quoted
    identifiers **disabled**, so ``"tbl"`` is parsed as a string literal
    and ``SELECT * FROM "tbl"`` fails with "unexpected token". A bare
    identifier works for the common snake_case / CamelCase Access table
    names; names containing spaces or punctuation are wrapped in
    Access-style square brackets (``[Drug Names]``), which UCanAccess
    understands. Embedded ``]`` is stripped defensively (Access itself
    forbids it in object names), which also removes the only
    bracket-injection vector.
    """
    if _SAFE_IDENTIFIER.match(name):
        return name
    sanitised = name.replace("]", "")
    return f"[{sanitised}]"


def _all_string_custom_schema(column_names: list[str]) -> str:
    """Return a Spark ``customSchema`` string typing every column as STRING.

    A raw ``copy`` moves a table verbatim and needs **no** numeric
    fidelity from the source, so every column is read as ``STRING``.
    Beyond keeping the copy lossless, this also insulates the read from
    Access column-type surprises: Spark then uses ``getString()`` for the
    whole row rather than a per-type getter (e.g. ``getInt()``), so it
    never trips on an unexpected value in a column Spark inferred as
    numeric.

    Column names are quoted with backticks (Spark's identifier quote)
    and any embedded backtick is doubled, which is both correct DDL and
    the only injection vector to close.

    Args:
        column_names: The column names, in order, as reported by the
            JDBC ``ResultSetMetaData``.

    Returns:
        A ``customSchema`` clause like ``` `col_a` STRING, `col_b` STRING ```.
    """
    return ", ".join(f"`{name.replace('`', '``')}` STRING" for name in column_names)


def _register_dialect(spark: SparkSession) -> None:
    """Register the UCanAccess ``JdbcDialect`` on the driver JVM (idempotent).

    UCanAccess/HSQLDB parses double-quoted identifiers as **string
    literals**, but Spark's default JDBC dialect quotes projected columns
    with double quotes (``SELECT "col" FROM t``). The result is that every
    Access column resolves to its own name for every row — a table read
    comes back filled with header names instead of data — and, when the
    column's Catalyst type is numeric, Spark's ``getInt()`` getter throws
    ``incompatible data type in conversion: from SQL type CHARACTER to
    java.lang.Integer, value: <column name>``.

    :data:`_DIALECT_CLASS` (compiled into
    ``ucanaccess-spark-dialect.jar`` at Docker build time) overrides
    ``quoteIdentifier`` to emit Access-style ``[col]`` brackets, which
    UCanAccess accepts as identifiers, so Spark's native — fast,
    distributed, lazy — JDBC reader returns correct values.

    The dialect is a *driver-side* concern: ``quoteIdentifier`` runs when
    Spark builds the pushdown SQL string on the driver, and only the
    finished SQL text is shipped to executors, so registering it here
    (before the read plan is built) is sufficient. ``registerDialect``
    prepends to the dialect list, so re-registering per read simply keeps
    ours highest-precedence; it is cheap and safe.

    Args:
        spark: Active Spark session whose JVM gateway hosts the dialect
            registry.

    Raises:
        RuntimeError: When the dialect class is not on the JVM classpath
            (the jar was not baked into the image). Surfaced eagerly so a
            misbuilt image fails loudly rather than silently reverting to
            the header-as-rows corruption.
    """
    jvm = spark.sparkContext._jvm
    if jvm is None:  # pragma: no cover - JVM always up in a live session
        raise RuntimeError(
            "No active JVM gateway on the Spark context; cannot register the UCanAccess dialect."
        )
    try:
        context_cl = jvm.java.lang.Thread.currentThread().getContextClassLoader()
        dialect_cls = jvm.java.lang.Class.forName(_DIALECT_CLASS, True, context_cl)
        jvm.org.apache.spark.sql.jdbc.JdbcDialects.registerDialect(dialect_cls.newInstance())
    except Exception as exc:
        raise RuntimeError(
            f"UCanAccess Spark dialect {_DIALECT_CLASS!r} is not on the "
            "JVM classpath. It is compiled at Docker build time into "
            "ucanaccess-spark-dialect.jar; rebuild the image "
            "(docker/Dockerfile) so the jar is baked into "
            "$SPARK_HOME/jars/."
        ) from exc


def read_table(
    spark: SparkSession,
    container_file_path: str,
    table_name: str,
    lowercase_columns: bool,
) -> DataFrame:
    """Read one Access table into a lazy Spark DataFrame via JDBC.

    Correct reads depend on :func:`_register_dialect`: UCanAccess reads
    Spark's default double-quoted column identifiers as string literals,
    which fills every row with the column names (and crashes ``getInt()``
    on numeric columns). The registered dialect makes Spark bracket
    identifiers (``[col]``), which UCanAccess resolves correctly, so the
    native distributed JDBC read returns real values.

    Every column is additionally read as ``STRING`` (via Spark's
    ``customSchema`` option) regardless of the Access column type: a
    ``copy`` job moves a table verbatim and needs no numeric fidelity, and
    all-STRING keeps the read robust against any Access type surprise. The
    column list comes from a lazy schema probe (Spark's JDBC schema
    resolution reads ``ResultSetMetaData``, not row data), which is then
    used to build the all-STRING ``customSchema``.

    Args:
        spark: Active Spark session.
        container_file_path: Container-visible path to the Access file.
        table_name: The Access table to read.
        lowercase_columns: Lowercase every column name when ``True``.

    Returns:
        Lazy Spark DataFrame for the requested table with every column
        typed as ``StringType``.
    """
    _register_dialect(spark)
    dbtable = _quote_identifier(table_name)
    url = _jdbc_url(container_file_path)

    def _reader() -> DataFrameReader:
        return (
            spark.read.format("jdbc")
            .option("url", url)
            .option("driver", _UCANACCESS_DRIVER)
            .option("dbtable", dbtable)
        )

    # Resolve the column names from metadata only (no row scan), then
    # re-open the reader forcing every column to STRING. ``.schema`` on a
    # JDBC relation is answered from ``ResultSetMetaData``.
    column_names = _reader().load().columns
    df: DataFrame = _reader().option("customSchema", _all_string_custom_schema(column_names)).load()
    if lowercase_columns:
        df = df.toDF(*[c.lower() for c in df.columns])
    return df


__all__ = [
    "get_active_session",
    "is_system_table",
    "list_tables",
    "read_table",
]

# ``_all_string_custom_schema`` is intentionally module-private (leading
# underscore) but is exercised directly by the unit tests.
