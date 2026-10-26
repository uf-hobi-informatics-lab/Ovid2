"""Spark-free Access reader helpers (table enumeration without a JVM Spark session).

The web / control-plane tier browses an Access ``.accdb`` / ``.mdb`` file
to list the user tables inside it, but it has **no active Spark session**
(and must stay install-light — no pyspark). This module opens the Access
file over the UCanAccess JDBC driver through :mod:`jaydebeapi` /
:mod:`jpype`, which spin up a *lightweight* JVM on demand and reuse the
exact same UCanAccess jars the Spark read path uses.

Deliberately free of any ``pyspark`` import so it can run in the web tier.
The heavy ``jaydebeapi`` / ``jpype`` imports are done lazily inside the
function body (installed via the ``access`` optional-dependency group).

The shared system-table filter (``is_system_table`` + its constant
tables) lives here so both this module and
:mod:`ovid2.drivers.access.spark` apply an identical policy.
"""

from __future__ import annotations

import contextlib
import glob
import os
import tempfile

#: UCanAccess JDBC driver class name (baked into the ETL image jars and,
#: for the web tier, resolved from :func:`ucanaccess_jars`).
UCANACCESS_DRIVER = "net.ucanaccess.jdbc.UcanaccessDriver"

#: Table-name prefixes/patterns that are UCanAccess / HSQLDB / Access
#: internals rather than user data. Matched case-insensitively.
SYSTEM_TABLE_PREFIXES: tuple[str, ...] = ("msys", "~", "f_")

#: HSQLDB schemas UCanAccess exposes that must never be surfaced as
#: user tables.
SYSTEM_SCHEMAS: frozenset[str] = frozenset({"information_schema", "system_lobs", "public"})


#: Default container / image path where the UCanAccess jars are baked.
#: Derived from ``$SPARK_HOME`` (the Bitnami base image sets it) rather
#: than hardcoding the image layout; overridable via
#: ``$OVID2_UCANACCESS_JARS``.
def _default_jar_dir() -> str | None:
    """Return the image's default Spark jar directory, if discoverable."""
    spark_home = os.environ.get("SPARK_HOME")
    if spark_home:
        return os.path.join(spark_home, "jars")
    return None


def is_system_table(name: str, schema: str | None = None) -> bool:
    """Return ``True`` when ``name`` / ``schema`` is an engine internal.

    Filters MSys* Access system tables, UCanAccess ``~`` temporaries,
    and the HSQLDB metadata schemas UCanAccess overlays on top of the
    Access file.

    Args:
        name: The candidate table name.
        schema: The JDBC ``TABLE_SCHEM`` for the table, when known.

    Returns:
        ``True`` when the table should be hidden from users.
    """
    if schema and schema.strip().lower() in SYSTEM_SCHEMAS:
        return True
    lowered = name.strip().lower()
    return any(lowered.startswith(prefix) for prefix in SYSTEM_TABLE_PREFIXES)


def _mirror_folder() -> str:
    """Return a writable directory for UCanAccess's HSQLDB mirror files.

    Mirrors :func:`ovid2.drivers.access.spark._mirror_folder` so the
    web-tier browse and the Spark read path spill the on-disk HSQLDB
    copy to the same (or an equivalently writable) scratch location and
    never write next to a read-only-mounted ``.accdb``. Honours
    ``$OVID2_ACCESS_MIRROR_DIR`` when set.
    """
    override = os.environ.get("OVID2_ACCESS_MIRROR_DIR")
    base = override or os.path.join(tempfile.gettempdir(), "ovid2_access_mirror")
    os.makedirs(base, exist_ok=True)
    return base


def jdbc_url(file_path: str) -> str:
    """Return the UCanAccess JDBC URL for an Access file.

    ``memory=false`` keeps UCanAccess from loading the whole database
    into the JVM heap (large reference databases can exceed ~800 MB), ``skipIndexes=true``
    avoids rebuilding every Access index on open, and ``mirrorFolder``
    redirects the HSQLDB mirror to a writable temp dir so the source
    (typically on a read-only mount) is never written to.

    Args:
        file_path: Path to the ``.accdb`` / ``.mdb`` file visible to
            this process.

    Returns:
        The full ``jdbc:ucanaccess://...`` URL.
    """
    mirror = _mirror_folder()
    return (
        f"jdbc:ucanaccess://{file_path}"
        ";memory=false;skipIndexes=true;openExclusive=false"
        f";mirrorFolder={mirror}"
    )


def ucanaccess_jars() -> list[str]:
    """Return the UCanAccess (+ dependency) jar paths for the web tier.

    Resolution order:

    1. ``$OVID2_UCANACCESS_JARS`` — an ``os.pathsep``-separated list of
       jar files or directories (directories are globbed for ``*.jar``).
    2. ``$SPARK_HOME/jars`` when ``$SPARK_HOME`` is set and present (the
       ETL image default).

    Returns:
        A list of absolute jar paths (possibly empty when no jars are
        discoverable — the caller degrades gracefully to "browsing not
        supported").
    """
    jars: list[str] = []
    override = os.environ.get("OVID2_UCANACCESS_JARS")
    candidates: list[str] = []
    if override:
        candidates.extend(part for part in override.split(os.pathsep) if part)
    default_dir = _default_jar_dir()
    if default_dir and os.path.isdir(default_dir):
        candidates.append(default_dir)
    for candidate in candidates:
        if os.path.isdir(candidate):
            jars.extend(sorted(glob.glob(os.path.join(candidate, "*.jar"))))
        elif os.path.isfile(candidate) and candidate.endswith(".jar"):
            jars.append(candidate)
    # De-duplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for jar in jars:
        if jar not in seen:
            seen.add(jar)
            unique.append(jar)
    return unique


def list_tables_no_spark(file_path: str) -> list[str]:
    """Enumerate the user tables in an Access file *without* Spark.

    Opens the file over the UCanAccess JDBC driver via
    :mod:`jaydebeapi` (which starts a lightweight JVM through
    :mod:`jpype`), reads ``DatabaseMetaData.getTables(..., ["TABLE"])``,
    filters engine internals, and returns the user tables sorted
    case-insensitively.

    Args:
        file_path: Path to the ``.accdb`` / ``.mdb`` file visible to
            this process.

    Returns:
        Sorted list of user-table names.

    Raises:
        NotImplementedError: When ``jaydebeapi`` is not installed or the
            UCanAccess jars cannot be located — the caller treats this
            as "table browsing unavailable in this tier" and stops at
            the file level (rather than erroring hard).
        RuntimeError: When the JDBC driver cannot open the file.
    """
    try:
        import jaydebeapi
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise NotImplementedError(
            "listing Access tables in the web tier requires the "
            "'access' extra (pip install 'ovid2[access]'); falling back "
            "to file-level browse."
        ) from exc

    jars = ucanaccess_jars()
    if not jars:  # pragma: no cover - env-dependent
        raise NotImplementedError(
            "UCanAccess jars not found; set $OVID2_UCANACCESS_JARS to a "
            "jar directory to enable Access table browsing in the web tier."
        )

    connection = None
    try:
        connection = jaydebeapi.connect(
            UCANACCESS_DRIVER,
            jdbc_url(file_path),
            [],
            jars,
        )
        jconn = connection.jconn  # underlying java.sql.Connection
        metadata = jconn.getMetaData()
        result_set = metadata.getTables(None, None, "%", ["TABLE"])
        tables: list[str] = []
        try:
            while result_set.next():
                schema = result_set.getString("TABLE_SCHEM")
                name = result_set.getString("TABLE_NAME")
                if name and not is_system_table(name, schema):
                    tables.append(name)
        finally:
            result_set.close()
    except NotImplementedError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Failed to enumerate Access tables in {file_path!r}: {exc}") from exc
    finally:
        if connection is not None:
            with contextlib.suppress(Exception):
                connection.close()
    return sorted(tables, key=str.lower)


__all__ = [
    "SYSTEM_SCHEMAS",
    "SYSTEM_TABLE_PREFIXES",
    "UCANACCESS_DRIVER",
    "is_system_table",
    "jdbc_url",
    "list_tables_no_spark",
    "ucanaccess_jars",
]
