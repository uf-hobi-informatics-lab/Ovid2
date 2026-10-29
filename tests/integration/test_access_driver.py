"""Opt-in integration test for the Access (.accdb) driver.

Reads a **real** Microsoft Access file end-to-end through
:class:`ovid2.drivers.access.driver.AccessDriver`, proving that (a)
every user table is enumerated and (b) each table reads as a separate
DataFrame. Skipped by default because it needs both a live Spark JVM
*and* the UCanAccess jars on the classpath — neither of which is
present in the default dev shell.

Enable it by pointing ``OVID2_TEST_ACCDB`` at a readable ``.accdb`` /
``.mdb`` file and ``OVID2_TEST_UCANACCESS_JARS`` at a colon-separated
list of the UCanAccess + jackcess + hsqldb + commons-lang3 jars (the
same jars baked into ``docker/Dockerfile``). Inside the ETL image the
jars are already on the classpath, so only ``OVID2_TEST_ACCDB`` is
required there.

Example (host with jars downloaded to /tmp/uca):

    OVID2_TEST_ACCDB="/path/to/sample.accdb" \\
    OVID2_TEST_UCANACCESS_JARS="/tmp/uca/ucanaccess-5.0.1.jar:/tmp/uca/jackcess-4.0.5.jar:/tmp/uca/hsqldb-2.5.0.jar:/tmp/uca/commons-lang3-3.12.0.jar" \\
    pytest tests/integration/test_access_driver.py -m integration -q
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.integration

# The module-scoped Spark session's py4j gateway leaves a client socket for
# the JVM to close on teardown; CPython's GC reports it as a
# ``ResourceWarning`` which the repo's ``filterwarnings = ["error"]`` would
# otherwise escalate to a test error. The socket is owned by pyspark's
# gateway (not ovid2 code), so silence just that warning for this opt-in,
# real-Spark module.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.filterwarnings("ignore::ResourceWarning"),
]

_ACCDB = os.environ.get("OVID2_TEST_ACCDB")
_JARS = os.environ.get("OVID2_TEST_UCANACCESS_JARS")


@pytest.fixture(scope="module")
def access_spark():
    """Spark session with the UCanAccess jars added (opt-in)."""
    if not _ACCDB or not os.path.isfile(_ACCDB):
        pytest.skip("OVID2_TEST_ACCDB unset or not a file.")
    pytest.importorskip("pyspark")
    from pyspark.sql import SparkSession

    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    builder = (
        SparkSession.builder.master("local[2]")
        .appName("ovid2-access-it")
        .config("spark.ui.enabled", "false")
    )
    if _JARS:
        # OVID2_TEST_UCANACCESS_JARS is os.pathsep-separated (matching a
        # classpath), but ``spark.jars`` expects a comma-separated list.
        jar_list = ",".join(p for p in _JARS.split(os.pathsep) if p)
        builder = builder.config("spark.jars", jar_list)
    session = builder.getOrCreate()
    session.sparkContext.setLogLevel("ERROR")
    # Fail fast with a clear message when the driver class is absent
    # rather than deep inside the read. Resolve via the thread context
    # classloader — the system classloader used by a bare ``Class.forName``
    # cannot see jars added through ``spark.jars`` / ``--jars``.
    try:
        jvm = session.sparkContext._jvm
        context_cl = jvm.java.lang.Thread.currentThread().getContextClassLoader()
        jvm.java.lang.Class.forName("net.ucanaccess.jdbc.UcanaccessDriver", True, context_cl)
    except Exception:
        session.stop()
        pytest.skip("UCanAccess driver not on the classpath; set OVID2_TEST_UCANACCESS_JARS.")
    try:
        yield session
    finally:
        session.stop()


def _driver():
    from ovid2.drivers.access.driver import AccessDriver

    directory = os.path.dirname(_ACCDB)
    filename = os.path.basename(_ACCDB)
    return AccessDriver(
        {"host_path": directory, "container_path": directory, "mount_mode": "ro"},
        {},
        {"path": filename},
    )


def test_enumerates_user_tables(access_spark) -> None:
    """The file exposes at least one user table, none of them system."""
    driver = _driver()
    tables = driver.tables_to_read(access_spark)
    assert tables, "expected at least one user table in the .accdb file"
    lowered = [t.lower() for t in tables]
    assert not any(t.startswith("msys") for t in lowered)
    assert not any(t.startswith("~") for t in lowered)


def test_each_table_reads_as_separate_dataframe(access_spark) -> None:
    """Every enumerated table reads into its own non-empty-schema DataFrame."""
    driver = _driver()
    tables = driver.tables_to_read(access_spark)
    # Cap the number of tables materialised so the test stays quick on a
    # large feed; reading the schema (df.columns) is enough to prove the
    # per-table read path works without scanning all 800MB.
    for table in tables[:5]:
        df = driver.read(access_spark, "multum", "batch", table, table_name=table)
        assert df.columns, f"table {table} read with no columns"


def test_read_forces_every_column_to_string(access_spark) -> None:
    """Every column reads as StringType.

    A ``copy`` moves a table verbatim and needs no numeric fidelity, and
    all-STRING keeps the read robust against Access column-type surprises
    (Spark uses ``getString()`` for the whole row rather than a per-type
    getter such as ``getInt()``).
    """
    from pyspark.sql.types import StringType

    driver = _driver()
    tables = driver.tables_to_read(access_spark)
    for table in tables[:5]:
        df = driver.read(access_spark, "multum", "batch", table, table_name=table)
        assert all(isinstance(field.dataType, StringType) for field in df.schema), (
            f"table {table} has a non-string column after read"
        )


def test_read_returns_values_not_column_names(access_spark) -> None:
    """Regression: rows must carry real data, not the column names.

    UCanAccess parses double-quoted identifiers as string literals, and
    Spark's default JDBC dialect double-quotes projected columns, so
    without the bracket-quoting ``UcanaccessDialect`` (baked into the
    image, registered by ``read_table``) every row comes back filled with
    its own column names. This asserts at least one materialised row
    differs from the header, which is exactly the corruption this fix
    addresses. Requires the dialect jar on the classpath (present in the
    image; add it to ``OVID2_TEST_UCANACCESS_JARS`` when running on a
    host).
    """
    driver = _driver()
    tables = driver.tables_to_read(access_spark)
    checked = 0
    for table in tables:
        df = driver.read(access_spark, "multum", "batch", table, table_name=table)
        cols = df.columns
        # Pull a small sample without a full scan; skip genuinely empty
        # tables (no rows to compare against the header).
        sample = df.take(5)
        if not sample:
            continue
        checked += 1
        for row in sample:
            values = [row[c] for c in cols]
            # The bug produced values == the (lowercased) column names for
            # every column of every row. A correct read has at least one
            # value that is not simply its column's name.
            assert any(
                (v is None) or (str(v).lower() != c.lower())
                for c, v in zip(cols, values, strict=False)
            ), (
                f"table {table} row {values!r} equals its column names "
                f"{cols!r} — the UCanAccess header-as-rows corruption is "
                "back (dialect not registered / jar missing?)."
            )
        if checked >= 5:
            break
    assert checked, "no non-empty table found to verify row values"


def test_list_dir_surfaces_tables(access_spark) -> None:
    """list_dir on the file returns kind='table' entries (one per table)."""
    driver = _driver()
    filename = os.path.basename(_ACCDB)
    entries = driver.list_dir(filename)
    assert entries
    assert all(e.kind == "table" for e in entries)
    # path round-trips as <file>/<table> (a valid Data Space path)
    assert all(e.path == f"{filename}/{e.name}" for e in entries)
