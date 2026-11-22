"""``ovid2 mcp`` — launch the MCP server.

W6 ships the real server (see :mod:`ovid2.mcp.server`). This module is
purely the argparse glue: it validates the ``--transport`` choice and
hands control to :func:`ovid2.mcp.server.run_stdio` or
:func:`ovid2.mcp.server.run_http`.

The registered subcommand is discovered by :mod:`ovid2.cli.__main__`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ovid2.mcp.settings import MCP_BIND, MCP_UPSTREAM


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``mcp`` subcommand on the top-level parser."""
    parser = subparsers.add_parser(
        "mcp",
        help="Run the MCP server that wraps the web REST API.",
        description=(
            "Model-Context-Protocol server that exposes the Ovid2 REST "
            "API to LLM agents. Reads/writes flow through the ovid2 "
            "web control panel."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="MCP transport (default: stdio).",
    )
    parser.add_argument(
        "--bind",
        default=MCP_BIND,
        help=f"HTTP bind host:port (default: {MCP_BIND}). Ignored for stdio.",
    )
    parser.add_argument(
        "--insecure-allow-network",
        action="store_true",
        dest="insecure_network",
        help=(
            "Allow the HTTP transport on a non-loopback address. The MCP HTTP "
            "endpoint does not authenticate its clients and acts with its own "
            "service token, so only expose it behind an authenticating proxy."
        ),
    )
    parser.add_argument(
        "--upstream",
        default=MCP_UPSTREAM,
        help=f"Base URL of the ovid2-web REST API (default: {MCP_UPSTREAM}).",
    )
    parser.set_defaults(_command="mcp", _entry=main)


def _is_loopback(host: str) -> bool:
    """Return whether ``host`` is ``localhost`` or a loopback IP."""
    import ipaddress

    value = host.strip().strip("[]")
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def main(args: argparse.Namespace) -> int:
    """Entry point invoked by :mod:`ovid2.cli.__main__`."""
    try:
        from ovid2.mcp import server as mcp_server
    except ImportError as exc:  # pragma: no cover - depends on `mcp` extra
        print(
            f"[ovid2 mcp] The MCP SDK is not installed: {exc}\n"
            "[ovid2 mcp] Install the optional extra with `pip install ovid2[mcp]`.",
            file=sys.stderr,
        )
        return 1

    if args.transport == "stdio":
        return asyncio.run(mcp_server.run_stdio(upstream=args.upstream))
    if args.transport == "http":
        host = args.bind.rpartition(":")[0] or "127.0.0.1"
        if not _is_loopback(host) and not getattr(args, "insecure_network", False):
            print(
                f"[ovid2 mcp] refusing to serve HTTP on {host}: the MCP endpoint does not "
                "authenticate clients and calls the API with its own token. Bind "
                "127.0.0.1 (front it with an authenticating proxy) or pass "
                "--insecure-allow-network.",
                file=sys.stderr,
            )
            return 2
        return asyncio.run(mcp_server.run_http(bind=args.bind, upstream=args.upstream))
    # argparse already restricts the choices; the branch below is a
    # defensive fallback for typos.
    print(f"[ovid2 mcp] Unknown transport '{args.transport}'.", file=sys.stderr)
    return 2
