"""The top-level ``ovid2`` parser must build on every supported Python.

Regression: after the partner -> pipeline rename the deprecated alias was
also registered as ``pipeline``. Python 3.11+ ``argparse`` rejects a
duplicate subparser name (``conflicting subparser``), so *every* ``ovid2``
invocation crashed on 3.11/3.12 while 3.10 hosts kept working.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from ovid2.cli.__main__ import _build_parser


@pytest.mark.unit
def test_parser_builds_and_alias_is_distinct() -> None:
    parser = _build_parser()
    args = parser.parse_args(["pipeline", "list"])
    assert args._command == "pipeline"
    alias = parser.parse_args(["partner", "validate", "--partner", "demo"])
    assert alias._command == "partner"
    assert alias.pipeline == "demo"
    tool = parser.parse_args(["tools", "new-pipeline", "--name", "x", "--template", "t"])
    assert tool._tool == "new-pipeline"
    old = parser.parse_args(["tools", "new-partner", "--name", "x", "--template", "t"])
    assert old._tool == "new-partner"


@pytest.mark.unit
def test_cli_help_runs_in_a_fresh_interpreter() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "ovid2.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert "pipeline" in out.stdout
    assert "partner" not in out.stdout  # hidden alias stays out of the listing
