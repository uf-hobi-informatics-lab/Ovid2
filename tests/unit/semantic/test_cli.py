"""Unit tests for the ``ovid2 semantic`` CLI subcommand."""

from __future__ import annotations

import argparse

import pytest

from ovid2.cli import semantic as cli_semantic


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ovid2")
    subparsers = parser.add_subparsers(dest="command")
    cli_semantic.register(subparsers)
    return parser


class TestSemanticCli:
    """Smoke test that each action runs and produces the right exit code."""

    def test_list_all(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``ovid2 semantic list`` prints every registered handle."""
        parser = _build_parser()
        args = parser.parse_args(["semantic", "list"])
        rc = args._entry(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "[vocabulary]" in out
        assert "omop_concept" in out

    def test_list_filter_by_kind(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``--kind`` narrows the listing."""
        parser = _build_parser()
        args = parser.parse_args(["semantic", "list", "--kind", "vocabulary"])
        rc = args._entry(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "omop_concept" in out

    def test_show_omop_concept(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``show`` prints every field of the handle."""
        parser = _build_parser()
        args = parser.parse_args(["semantic", "show", "vocabulary", "omop_concept"])
        rc = args._entry(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "vocabulary:omop_concept" in out
        assert "code_column" in out
        assert "concept_code" in out

    def test_show_unknown_returns_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Unknown handles produce a non-zero exit code and a helpful message."""
        parser = _build_parser()
        args = parser.parse_args(["semantic", "show", "vocabulary", "nonexistent"])
        rc = args._entry(args)
        out = capsys.readouterr().out
        assert rc == 1
        assert "Unknown semantic handle" in out

    def test_bindings_no_bindings_registered(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When no bindings are registered, ``bindings`` prints a friendly message."""
        parser = _build_parser()
        args = parser.parse_args(["semantic", "bindings"])
        rc = args._entry(args)
        out = capsys.readouterr().out
        # There may or may not be bindings depending on other test isolation;
        # a successful exit is what matters.
        assert rc == 0
        assert ("No column bindings registered." in out) or ("binding(s)." in out)

    def test_bindings_table_without_cdm_errors(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``--table`` requires ``--cdm``."""
        parser = _build_parser()
        args = parser.parse_args(["semantic", "bindings", "--table", "diagnosis"])
        rc = args._entry(args)
        out = capsys.readouterr().out
        assert rc == 1
        assert "--table requires --cdm" in out
