"""CLI unit tests for the ``ovid2 mcp`` subcommand."""

from __future__ import annotations

import argparse

import pytest


@pytest.mark.unit
def test_register_adds_mcp_subparser() -> None:
    """``register`` wires the subcommand and its flags on the parser."""
    from ovid2.mcp.cli import register

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    register(subparsers)

    args = parser.parse_args(["mcp", "--transport", "stdio"])
    assert args.command == "mcp"
    assert args.transport == "stdio"
    assert args.bind
    assert args.upstream


@pytest.mark.unit
def test_register_defaults_are_from_settings() -> None:
    """The default bind/upstream come from :mod:`ovid2.mcp.settings`."""
    from ovid2.mcp import settings
    from ovid2.mcp.cli import register

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    register(subparsers)
    args = parser.parse_args(["mcp"])
    assert args.bind == settings.MCP_BIND
    assert args.upstream == settings.MCP_UPSTREAM


@pytest.mark.unit
def test_main_rejects_unknown_transport(capsys: pytest.CaptureFixture[str]) -> None:
    """Argparse should refuse an unknown ``--transport`` value up front."""
    from ovid2.mcp.cli import register

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    register(subparsers)

    with pytest.raises(SystemExit):
        parser.parse_args(["mcp", "--transport", "bogus"])
    err = capsys.readouterr().err
    assert "invalid choice" in err.lower()
