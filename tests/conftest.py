"""Shared pytest fixtures.

Ensures ``src/`` is on ``sys.path`` when the tests are run without a prior
``pip install -e .``. Once installed via ``uv sync``, this shim is a no-op.

Provides a session-scoped ``spark`` fixture for unit and integration tests.
The fixture is auto-skipped when a JVM is not available (``java`` not on
``PATH`` and ``JAVA_HOME`` unset), so contract-only test runs still work
on hosts without Spark's Java runtime.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Make ``ovid2_pipelines.testpipeline`` visible to the whole session.
_FIXTURE_PIPELINES = Path(__file__).resolve().parent / "fixtures" / "pipelines_root"
if _FIXTURE_PIPELINES.is_dir() and str(_FIXTURE_PIPELINES) not in sys.path:
    sys.path.insert(0, str(_FIXTURE_PIPELINES))

# Make the in-repo real pipeline packages (``pipelines/ovid2_pipelines/*``) visible.
_REPO_PIPELINES = Path(__file__).resolve().parent.parent / "pipelines"
if _REPO_PIPELINES.is_dir() and str(_REPO_PIPELINES) not in sys.path:
    sys.path.insert(0, str(_REPO_PIPELINES))


def _java_available() -> bool:
    """Return True if a JVM appears to be available."""
    if os.environ.get("JAVA_HOME"):
        return True
    return shutil.which("java") is not None


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Return the absolute path to the repository root."""
    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def src_root(repo_root: Path) -> Path:
    """Return the path to ``src/ovid2/`` for source-tree walkers."""
    return repo_root / "src" / "ovid2"


@pytest.fixture(scope="session")
def spark() -> Iterator[object]:
    """Session-scoped Spark session; auto-skips when no JVM is available."""
    if not _java_available():
        pytest.skip("No JVM available (JAVA_HOME unset and 'java' not on PATH).")

    pytest.importorskip("pyspark")
    from pyspark.sql import SparkSession

    # Python workers must match the driver's minor version; without this
    # Spark spawns ``python3`` from PATH (possibly an older system Python).
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    from ovid2.config.settings import SPARK_CONFIG

    session = (
        SparkSession.builder.master("local[2]")
        .appName("ovid2-tests")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "4")
        # Same SQL semantics as production (Spark 4 defaults ANSI on).
        .config("spark.sql.ansi.enabled", SPARK_CONFIG["spark.sql.ansi.enabled"])
        .getOrCreate()
    )
    session.sparkContext.setLogLevel("ERROR")
    try:
        yield session
    finally:
        session.stop()
