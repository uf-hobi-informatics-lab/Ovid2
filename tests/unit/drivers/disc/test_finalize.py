"""Unit tests for the disc single-file finalize path.

Covers the parts of :mod:`ovid2.drivers.disc.spark` that were rewritten
in the 2026-08 single-CSV finalize refactor:

- ``_merge_csv_part_files``: cat-based concat of ``_header.csv`` +
  ``part-*.csv``, with a pure-Python fallback when ``cat`` is absent.
- ``_maybe_coalesce``: honours ``$OVID2_DISC_WRITE_PARTITIONS``,
  otherwise no-op.
- End-to-end write via a real Spark session (uses the shared ``spark``
  fixture; auto-skips when a JVM is unavailable).
- ``single_file=False`` regression path: writes the Spark-native
  part-directory layout, no ``_header.csv``.
"""

from __future__ import annotations

import csv
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Module-level helpers — no Spark required.
# ---------------------------------------------------------------------------


class TestMergeCsvPartFiles:
    """Coverage for the internal ``cat``-based CSV finalizer."""

    def _stage(self, work_dir: Path, header: str, parts: list[str]) -> None:
        """Create the staging directory shape ``_write_csv`` produces."""
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "_header.csv").write_text(header)
        for i, body in enumerate(parts):
            # Spark's part-file naming: ``part-00000-<uuid>.csv``.
            (work_dir / f"part-{i:05d}-abcd.csv").write_text(body)

    def test_concats_header_then_parts_in_sorted_order(
        self,
        tmp_path: Path,
    ) -> None:
        from ovid2.drivers.disc.spark import _merge_csv_part_files

        work = tmp_path / "tmp_out"
        self._stage(
            work,
            header="a,b\n",
            parts=["1,2\n", "3,4\n", "5,6\n"],
        )
        out = tmp_path / "out.csv"
        _merge_csv_part_files(str(work), str(out))
        assert out.read_text() == "a,b\n1,2\n3,4\n5,6\n"

    def test_deterministic_sort_of_parts(self, tmp_path: Path) -> None:
        from ovid2.drivers.disc.spark import _merge_csv_part_files

        work = tmp_path / "tmp_out"
        work.mkdir()
        # Create parts out of order on disk (fs order != our order).
        (work / "_header.csv").write_text("c\n")
        (work / "part-00007-x.csv").write_text("7\n")
        (work / "part-00000-x.csv").write_text("0\n")
        (work / "part-00003-x.csv").write_text("3\n")

        out = tmp_path / "out.csv"
        _merge_csv_part_files(str(work), str(out))
        # Sorted lex order: 00000, 00003, 00007.
        assert out.read_text() == "c\n0\n3\n7\n"

    def test_missing_header_raises(self, tmp_path: Path) -> None:
        from ovid2.drivers.disc.spark import _merge_csv_part_files

        work = tmp_path / "tmp_out"
        work.mkdir()
        (work / "part-00000-x.csv").write_text("1\n")
        with pytest.raises(FileNotFoundError, match="Missing header stage"):
            _merge_csv_part_files(str(work), str(tmp_path / "o.csv"))

    def test_missing_parts_raises(self, tmp_path: Path) -> None:
        from ovid2.drivers.disc.spark import _merge_csv_part_files

        work = tmp_path / "tmp_out"
        work.mkdir()
        (work / "_header.csv").write_text("c\n")
        with pytest.raises(FileNotFoundError, match="No part files"):
            _merge_csv_part_files(str(work), str(tmp_path / "o.csv"))

    def test_pure_python_fallback_when_cat_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Simulate an environment without ``cat`` on ``PATH``."""
        from ovid2.drivers.disc import spark as disc_spark

        work = tmp_path / "tmp_out"
        self._stage(
            work,
            header="h1,h2\n",
            parts=["a,b\n", "c,d\n"],
        )
        # Force ``shutil.which('cat')`` to report absent.
        monkeypatch.setattr(disc_spark.shutil, "which", lambda _n: None)
        out = tmp_path / "out.csv"
        disc_spark._merge_csv_part_files(str(work), str(out))
        assert out.read_text() == "h1,h2\na,b\nc,d\n"

    def test_binary_safe_pass_through(self, tmp_path: Path) -> None:
        """Body bytes are never re-encoded — CSV cell contents survive."""
        from ovid2.drivers.disc.spark import _merge_csv_part_files

        work = tmp_path / "tmp_out"
        work.mkdir()
        # UTF-8 with a multibyte char.
        (work / "_header.csv").write_bytes(b"col\n")
        (work / "part-00000-x.csv").write_bytes("é\n".encode())

        out = tmp_path / "out.csv"
        _merge_csv_part_files(str(work), str(out))
        assert out.read_bytes() == "col\né\n".encode()


class TestMaybeCoalesce:
    """The operator-tunable coalesce hook."""

    def test_no_coalesce_by_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ovid2.drivers.disc import spark as disc_spark

        monkeypatch.setattr(disc_spark, "DISC_WRITE_PARTITIONS", None)

        class FakeDF:
            def coalesce(self, n: int) -> FakeDF:
                raise AssertionError(
                    f"coalesce({n}) should not have been called when DISC_WRITE_PARTITIONS is None."
                )

        df = FakeDF()
        assert disc_spark._maybe_coalesce(df) is df  # type: ignore[arg-type]

    def test_coalesces_when_configured(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ovid2.drivers.disc import spark as disc_spark

        monkeypatch.setattr(disc_spark, "DISC_WRITE_PARTITIONS", 8)

        calls: list[int] = []

        class FakeDF:
            def coalesce(self, n: int) -> FakeDF:
                calls.append(n)
                return self

        df = FakeDF()
        result = disc_spark._maybe_coalesce(df)  # type: ignore[arg-type]
        assert result is df
        assert calls == [8]

    def test_treats_zero_as_no_op(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ovid2.drivers.disc import spark as disc_spark

        monkeypatch.setattr(disc_spark, "DISC_WRITE_PARTITIONS", 0)

        class FakeDF:
            def coalesce(self, n: int) -> FakeDF:  # pragma: no cover
                raise AssertionError("should not coalesce on 0")

        df = FakeDF()
        assert disc_spark._maybe_coalesce(df) is df  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# End-to-end via a real Spark session.
# ---------------------------------------------------------------------------


class TestEndToEndWrite:
    """Real PySpark writes hitting the ``_write_csv`` code path.

    Verifies that:

    1. The default (``single_file=True``) path produces one merged
       ``.csv`` file with exactly one header line and the union of
       body rows.
    2. The opt-out (``single_file=False``) path leaves the raw Spark
       part-directory in place.
    3. Neither path applies ``coalesce`` when
       ``DISC_WRITE_PARTITIONS`` is ``None`` (the default).
    """

    def _rows(self) -> list[tuple[int, str]]:
        return [(1, "a"), (2, "b"), (3, "c"), (4, "d")]

    def test_single_file_write_produces_one_merged_csv(
        self,
        spark: object,
        tmp_path: Path,
    ) -> None:
        from ovid2.drivers.disc.spark import write_dataframe

        df = spark.createDataFrame(  # type: ignore[attr-defined]
            self._rows(),
            schema=["id", "letter"],
        )
        root = tmp_path / "root"
        write_dataframe(
            df,
            str(root),
            "demo",
            "20260101",
            "letters",
            delimiter=",",
            fmt="csv",
            single_file=True,
        )

        out = root / "demo" / "20260101" / "letters.csv"
        assert out.is_file(), f"Expected a single file at {out}"
        text = out.read_text().strip().splitlines()
        assert text[0] == "id,letter"
        data = sorted(text[1:])
        assert data == ["1,a", "2,b", "3,c", "4,d"]

        # Staging directory was cleaned up.
        staging = root / "demo" / "20260101" / "tmp_letters.csv"
        assert not staging.exists()

    def test_single_file_write_via_csv_module_matches_body(
        self,
        spark: object,
        tmp_path: Path,
    ) -> None:
        """Round-trip through Python's csv module: same row count + values."""
        from ovid2.drivers.disc.spark import write_dataframe

        df = spark.createDataFrame(  # type: ignore[attr-defined]
            self._rows(),
            schema=["id", "letter"],
        )
        root = tmp_path / "root"
        write_dataframe(
            df,
            str(root),
            "p",
            "b",
            "t",
            ",",
            "csv",
            single_file=True,
        )
        out = root / "p" / "b" / "t.csv"
        with out.open() as fh:
            rows = list(csv.reader(fh))
        assert rows[0] == ["id", "letter"]
        body = sorted((int(r[0]), r[1]) for r in rows[1:])
        assert body == self._rows()

    def test_multi_file_write_leaves_part_directory(
        self,
        spark: object,
        tmp_path: Path,
    ) -> None:
        from ovid2.drivers.disc.spark import write_dataframe

        df = spark.createDataFrame(  # type: ignore[attr-defined]
            self._rows(),
            schema=["id", "letter"],
        )
        root = tmp_path / "root"
        write_dataframe(
            df,
            str(root),
            "demo",
            "20260101",
            "letters",
            delimiter=",",
            fmt="csv",
            single_file=False,
        )

        # With single_file=False the output is a directory of parts.
        out = root / "demo" / "20260101" / "letters.csv"
        assert out.is_dir(), f"Expected a directory (Spark part-layout) at {out}"
        parts = sorted(out.glob("part-*.csv"))
        assert parts, "Spark should have produced at least one part file."
        # Every part carries its own header row (Spark default).
        for p in parts:
            first = p.read_text().splitlines()[0]
            assert first == "id,letter"

    def test_write_dispatch_error_on_unknown_format(
        self,
        spark: object,
        tmp_path: Path,
    ) -> None:
        from ovid2.drivers.disc.spark import write_dataframe

        df = spark.createDataFrame(  # type: ignore[attr-defined]
            self._rows(),
            schema=["id", "letter"],
        )
        with pytest.raises(ValueError, match="Unsupported format"):
            write_dataframe(
                df,
                str(tmp_path),
                "p",
                "b",
                "t",
                delimiter=None,
                fmt="avro",
            )


# ---------------------------------------------------------------------------
# ``DiscDriver`` view — single_file threads through the merged location.
# ---------------------------------------------------------------------------


class TestDriverLocationView:
    """The driver's ``_merged_location_view`` must expose single_file."""

    def test_defaults_to_true_when_absent(self, tmp_path: Path) -> None:
        from ovid2.drivers.disc.driver import DiscDriver

        d = DiscDriver(args={"host_path": str(tmp_path)}, location={"path": "sub"})
        view = d._merged_location_view()
        assert view["single_file"] is True

    def test_honours_explicit_false(self, tmp_path: Path) -> None:
        from ovid2.drivers.disc.driver import DiscDriver

        d = DiscDriver(
            args={"host_path": str(tmp_path)},
            location={"path": "sub", "single_file": False},
        )
        assert d._merged_location_view()["single_file"] is False

    def test_honours_explicit_true(self, tmp_path: Path) -> None:
        from ovid2.drivers.disc.driver import DiscDriver

        d = DiscDriver(
            args={"host_path": str(tmp_path)},
            location={"single_file": True},
        )
        assert d._merged_location_view()["single_file"] is True

    def test_as_legacy_src_args_forwards_single_file(
        self,
        tmp_path: Path,
    ) -> None:
        from ovid2.drivers.disc.driver import DiscDriver

        d = DiscDriver(
            args={"host_path": str(tmp_path)},
            location={"single_file": False},
        )
        _, args = d.as_legacy_src_args()
        assert args["single_file"] is False


class TestLegacyAddressSingleFile:
    """``DiscAddress`` (legacy shim) accepts the widened config."""

    def test_default_is_true(self, tmp_path: Path) -> None:
        from ovid2.drivers.disc.legacy import DiscAddress

        a = DiscAddress({"path": str(tmp_path)})
        assert a.single_file is True

    def test_explicit_false(self, tmp_path: Path) -> None:
        from ovid2.drivers.disc.legacy import DiscAddress

        a = DiscAddress({"path": str(tmp_path), "single_file": False})
        assert a.single_file is False

    def test_write_threads_single_file(
        self,
        tmp_path: Path,
    ) -> None:
        """``DiscAddress.write`` forwards ``single_file`` to the impl."""
        from ovid2.drivers.disc.legacy import DiscAddress

        a = DiscAddress({"path": str(tmp_path), "single_file": False})
        seen: dict[str, object] = {}

        def fake_write_dataframe(
            df: object,
            root: str,
            pipeline: str,
            batch: str,
            table: str,
            delimiter: object,
            fmt: str,
            *,
            single_file: bool = True,
        ) -> None:
            seen["single_file"] = single_file
            seen["fmt"] = fmt
            seen["table"] = table

        with patch(
            "ovid2.drivers.disc.spark.write_dataframe",
            new=fake_write_dataframe,
        ):
            a.write(object(), object(), "demo", "20260101", "letters")
        assert seen == {"single_file": False, "fmt": "csv", "table": "letters"}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run with pytest.")
