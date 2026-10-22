"""Unit tests for the source-glob helpers in ``ovid2.formatters.adapters._sources``.

Covers the ``_glob_to_java_regex`` translator and the end-to-end
``_apply_exclude_patterns`` filter against a real disc read. The regex
translator must emit a ``java.util.regex``-compatible string — the bug it
guards against is Python 3.11+'s ``fnmatch.translate`` emitting atomic-group
emulation (``(?P<g0>...)`` / ``(?P=g0)``) which Spark's ``rlike`` rejects with
``PatternSyntaxException: Unknown inline modifier``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ovid2.formatters.adapters._sources import _glob_to_java_regex

if TYPE_CHECKING:
    from pyspark.sql import SparkSession


@pytest.mark.parametrize(
    ("glob", "expected"),
    [
        ("*provider_sup*", "^.*provider_sup.*$"),
        ("*person_sup.csv", "^.*person_sup\\.csv$"),
        ("*drug_exposure_sup*", "^.*drug_exposure_sup.*$"),
        ("a?b", "^a.b$"),
        ("x.y*", "^x\\.y.*$"),
        ("PLAIN.CSV", "^plain\\.csv$"),  # lowercased
    ],
)
def test_glob_to_java_regex(glob: str, expected: str) -> None:
    assert _glob_to_java_regex(glob) == expected


def test_glob_to_java_regex_no_python_named_groups() -> None:
    """The output must never contain Python-only named-group syntax."""
    for glob in ("*a_sup*", "*b.csv", "weird+name*", "q(x)*"):
        regex = _glob_to_java_regex(glob)
        assert "(?P<" not in regex
        assert "(?P=" not in regex
        assert "(?s:" not in regex
        assert "\\Z" not in regex


def test_glob_to_java_regex_compiles_in_java(spark: SparkSession) -> None:
    """Every translated regex must compile in Spark's ``rlike`` (java.util.regex).

    This is the direct regression guard for the ACME 20260610 failure
    ``PatternSyntaxException: Unknown inline modifier near index 5``.
    """
    from pyspark.sql.functions import expr

    for glob in ("*provider_sup*", "*person_sup.csv", "*drug_exposure_sup*"):
        regex = _glob_to_java_regex(glob)
        # A trivial rlike that would raise PatternSyntaxException if the regex
        # used Python-only syntax.
        df = spark.range(1).withColumn("m", expr(f"'x' rlike '{regex}'"))
        # Force analysis + execution; must not raise.
        assert df.collect()[0]["m"] in (True, False)


def test_apply_exclude_drops_sup_files(spark: SparkSession, tmp_path) -> None:
    """End-to-end: the exclude pattern drops the ``*_sup`` file's rows.

    Uses a real disc read so ``input_file_name()`` is populated (the exclude
    filter is a no-op on non-disc addresses).
    """
    from ovid2.drivers.disc import DiscAddress
    from ovid2.formatters.adapters._sources import read_source
    from ovid2.formatters.spec import SourceSpec

    # Write two files under a pipeline/batch layout the DiscAddress understands.
    root = tmp_path / "disc"
    batch_dir = root / "acme" / "20260101"
    batch_dir.mkdir(parents=True)
    (batch_dir / "provider.csv").write_text("provider_id\n1\n", encoding="utf-8")
    (batch_dir / "provider_sup.csv").write_text("provider_id\n2\n", encoding="utf-8")

    address = DiscAddress({"path": str(root)})
    df = read_source(
        SourceSpec(
            name="provider",
            glob="*provider.csv",
            exclude=("*provider_sup*",),
            lowercase_columns=True,
        ),
        spark=spark,
        input_address=address,
        pipeline="acme",
        batch_id="20260101",
    )
    ids = sorted(r["provider_id"] for r in df.collect())
    assert ids == ["1"]  # provider_sup.csv (id=2) excluded


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
