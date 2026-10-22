"""Contract: the CLI stays usable when an optional extra is not installed.

The lean ETL-executor image installs ``ovid2`` **without** the ``[web]``
extra, so ``argon2-cffi`` (imported at module top level by
``ovid2.web.auth.breakglass``) is absent. Building the top-level parser
must not import a failing web module in a way that takes down unrelated
subcommands — in particular ``ovid2 etl``, which is what a worker runs.

This test simulates the missing dependency by blocking ``import argon2``
and asserts:

1. ``_build_parser`` still succeeds.
2. ``ovid2 etl ...`` parses and dispatches to the real ``ovid2.cli.etl``.
3. ``ovid2 web`` degrades to a non-crashing "unavailable" stub.

Regression guard for the ``ModuleNotFoundError: No module named 'argon2'``
that killed every in-container invocation before this fix.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterator

import pytest


@pytest.fixture()
def _no_argon2(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make ``import argon2`` raise, mimicking the executor image."""
    real_import = builtins.__import__

    def _blocked(name: str, *args: object, **kwargs: object) -> object:
        if name == "argon2" or name.startswith("argon2."):
            raise ModuleNotFoundError("No module named 'argon2'")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", _blocked)
    # Drop any cached web modules so the block takes effect on re-import.
    import sys

    for mod in list(sys.modules):
        if mod.startswith("ovid2.web.auth") or mod == "ovid2.web.cli":
            monkeypatch.delitem(sys.modules, mod, raising=False)
    yield


@pytest.mark.contract
def test_parser_builds_without_argon2(_no_argon2: None) -> None:
    """The parser builds and ``etl`` still dispatches to the real handler."""
    from ovid2.cli import __main__ as cli_main

    # Force a fresh parser build under the argon2 block.
    parser = cli_main._build_parser()

    args = parser.parse_args(
        [
            "etl",
            "--pipeline",
            "acme_from_disc",
            "--batch_id",
            "20260610",
            "--input_address",
            "in",
            "--output_address",
            "out",
            "--engine",
            "pyspark",
        ]
    )
    assert getattr(args, "_command", None) == "etl"
    assert args._entry.__module__ == "ovid2.cli.etl"


@pytest.mark.contract
def test_web_command_degrades_gracefully(
    _no_argon2: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """``ovid2 web`` reports the missing extra instead of crashing."""
    from ovid2.cli import __main__ as cli_main

    parser = cli_main._build_parser()
    args = parser.parse_args(["web"])
    rc = int(args._entry(args))

    assert rc == 1
    err = capsys.readouterr().err
    assert "'web' is unavailable" in err
    assert "argon2" in err


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
