"""Unit tests for :mod:`ovid2.core.engines.pyspark`.

Focus on :meth:`PySparkEngine.test` — the probe used by the web panel's
"Test engine" button. These tests intentionally do **not** launch a real
JVM: they patch ``pyspark.sql.SparkSession`` and the JDK-discovery
helpers so they can run on any host (including CI images with no JDK).
"""

from __future__ import annotations

import os
import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from ovid2.core.engines.pyspark import PySparkEngine


# ---------------------------------------------------------------------------
# Preflight: no JDK visible at all
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_test_reports_missing_jdk_when_java_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """`java` not on PATH and no JAVA_HOME → ``missing_jdk`` without touching Spark."""
    # Ensure `pyspark` imports cleanly (the module itself is loaded during
    # `PySparkEngine.test` inside a `try`/`except ImportError`).
    monkeypatch.setitem(sys.modules, "pyspark", types.ModuleType("pyspark"))
    pyspark_sql = types.ModuleType("pyspark.sql")
    # If our code ever *did* touch SparkSession.builder in this path, the
    # test would blow up here — that's exactly what we want to assert.
    pyspark_sql.SparkSession = MagicMock(  # type: ignore[attr-defined]
        side_effect=AssertionError("SparkSession must not be touched when no JDK is visible"),
    )
    monkeypatch.setitem(sys.modules, "pyspark.sql", pyspark_sql)

    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setattr("ovid2.core.engines.pyspark.shutil.which", lambda _cmd: None)

    result = PySparkEngine().test()

    assert result.ok is False
    assert result.code == "missing_jdk"
    assert "JDK" in result.message
    assert "openjdk" in result.detail["hint"].lower()
    # No JVM launched → no stderr capture.
    assert "stderr_tail" not in result.detail


@pytest.mark.unit
def test_test_honors_java_home_over_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """``JAVA_HOME`` alone is enough to pass the preflight."""
    fake_session = MagicMock()
    fake_session.sql.return_value.collect.return_value = [(1,)]
    engine = PySparkEngine()
    monkeypatch.setattr(engine, "get_session", lambda: fake_session)

    # Fake pyspark so the ImportError branch does not fire.
    monkeypatch.setitem(sys.modules, "pyspark", types.ModuleType("pyspark"))
    fake_sql = types.ModuleType("pyspark.sql")
    fake_sql.SparkSession = MagicMock()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyspark.sql", fake_sql)

    monkeypatch.setenv("JAVA_HOME", "/opt/some-jdk")
    monkeypatch.setattr("ovid2.core.engines.pyspark.shutil.which", lambda _cmd: None)

    result = engine.test()

    assert result.ok is True
    assert result.code == "ok"


# ---------------------------------------------------------------------------
# Post-launch: JDK on PATH but JVM won't start
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_test_reports_missing_jdk_on_java_gateway_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """`java` present but ``getOrCreate`` raises the py4j gateway error → ``missing_jdk``.

    This is the common "wrong-arch JDK" / "broken openjdk-headless" case.
    We want the operator to see an actionable hint, not the raw py4j
    ``RuntimeError``.
    """
    monkeypatch.setenv("JAVA_HOME", "/usr/lib/jvm/java-17-openjdk-amd64")

    def _boom() -> Any:
        raise RuntimeError("Java gateway process exited before sending its port number")

    engine = PySparkEngine()
    monkeypatch.setattr(engine, "get_session", _boom)

    monkeypatch.setitem(sys.modules, "pyspark", types.ModuleType("pyspark"))
    fake_sql = types.ModuleType("pyspark.sql")
    fake_sql.SparkSession = MagicMock()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyspark.sql", fake_sql)

    result = engine.test()

    assert result.ok is False
    assert result.code == "missing_jdk"
    assert "Java gateway" in result.message
    assert result.detail["exception_type"] == "RuntimeError"
    assert "Java gateway" in result.detail["exception_message"]
    assert "reinstalling" in result.detail["hint"] or "known-good" in result.detail["hint"]


# ---------------------------------------------------------------------------
# Generic error path: unrelated exception is still surfaced with stderr tail
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_test_returns_error_code_for_unrelated_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-JVM failure keeps the existing ``error`` code."""
    monkeypatch.setenv("JAVA_HOME", "/usr/lib/jvm/java-17-openjdk-amd64")

    def _bad() -> Any:
        raise ValueError("something else broke")

    engine = PySparkEngine()
    monkeypatch.setattr(engine, "get_session", _bad)

    monkeypatch.setitem(sys.modules, "pyspark", types.ModuleType("pyspark"))
    fake_sql = types.ModuleType("pyspark.sql")
    fake_sql.SparkSession = MagicMock()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyspark.sql", fake_sql)

    result = engine.test()

    assert result.ok is False
    assert result.code == "error"
    assert result.detail["exception_type"] == "ValueError"
    assert result.detail["exception_message"] == "something else broke"


@pytest.mark.unit
def test_test_captures_stderr_tail_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """JVM stderr written during the probe is captured into ``detail['stderr_tail']``.

    We simulate the JVM's behaviour by writing to fd 2 inside the
    monkeypatched ``get_session`` before raising.
    """
    monkeypatch.setenv("JAVA_HOME", "/usr/lib/jvm/java-17-openjdk-amd64")

    payload = b"[JVM stderr] Error: LinkageError xyz\n"

    def _write_then_raise() -> Any:
        os.write(2, payload)
        raise RuntimeError("Java gateway process exited before sending its port number")

    engine = PySparkEngine()
    monkeypatch.setattr(engine, "get_session", _write_then_raise)

    monkeypatch.setitem(sys.modules, "pyspark", types.ModuleType("pyspark"))
    fake_sql = types.ModuleType("pyspark.sql")
    fake_sql.SparkSession = MagicMock()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyspark.sql", fake_sql)

    result = engine.test()

    assert result.ok is False
    assert result.code == "missing_jdk"
    assert "stderr_tail" in result.detail
    assert "LinkageError" in result.detail["stderr_tail"]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_test_reports_ok_when_select_1_returns_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full happy-path: JDK visible + SELECT 1 = 1 → ``ok``."""
    monkeypatch.setenv("JAVA_HOME", "/usr/lib/jvm/java-17-openjdk-amd64")

    fake_session = MagicMock()
    fake_session.sql.return_value.collect.return_value = [(1,)]
    engine = PySparkEngine()
    monkeypatch.setattr(engine, "get_session", lambda: fake_session)

    monkeypatch.setitem(sys.modules, "pyspark", types.ModuleType("pyspark"))
    fake_sql = types.ModuleType("pyspark.sql")
    fake_sql.SparkSession = MagicMock()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyspark.sql", fake_sql)

    result = engine.test()

    assert result.ok is True
    assert result.code == "ok"
    assert "SELECT 1" in result.message


@pytest.mark.unit
def test_test_reports_unreachable_when_pyspark_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No pyspark installed → ``unreachable`` (unchanged behaviour)."""
    # Prevent import: force ImportError on ``from pyspark.sql import SparkSession``.
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def _blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "pyspark.sql" or name.startswith("pyspark."):
            raise ImportError(f"blocked: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _blocked_import)
    # Also strip the module cache so the import actually re-runs.
    for mod in list(sys.modules):
        if mod == "pyspark" or mod.startswith("pyspark."):
            monkeypatch.delitem(sys.modules, mod, raising=False)

    result = PySparkEngine().test()

    assert result.ok is False
    assert result.code == "unreachable"
    assert "pyspark not importable" in result.message
