"""``ovid2 manual`` — build, check and serve the Ovid2 Manual.

Subcommands:

- ``ovid2 manual build [--out site/]`` — render the static site.
- ``ovid2 manual check`` — validate ``manual.yaml``, every page, every
  internal link / anchor, and the freshness of the generated reference.
- ``ovid2 manual gen-reference [--check]`` — regenerate the *Reference*
  chapter from the live registries (``--check`` only reports drift).
- ``ovid2 manual wiki [--out wiki/]`` — export a GitHub wiki tree
  (:mod:`ovid2.manual.wiki`).
- ``ovid2 manual serve [--port 8765]`` — build into a temp dir and serve
  it locally (``http.server``) for a quick preview.

The same pages are served live by the control panel at ``/manual``.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``manual`` subcommand and its actions."""
    parser = subparsers.add_parser(
        "manual",
        help="Build, check or preview the Ovid2 Manual.",
        description=(
            "Build, check or preview the Ovid2 Manual. The same pages are served "
            "live by the web control panel at /manual."
        ),
    )
    action = parser.add_subparsers(dest="action", metavar="ACTION", required=True)

    build = action.add_parser("build", help="Render the manual to a static site.")
    build.add_argument("--out", default="site", help="Output directory (replaced).")
    build.add_argument("--no-strict", action="store_true", help="Build even when links are broken.")
    build.set_defaults(_command="manual", _entry=_entry_build)

    check = action.add_parser(
        "check", help="Validate pages, links, anchors and reference freshness."
    )
    check.set_defaults(_command="manual", _entry=_entry_check)

    gen = action.add_parser(
        "gen-reference", help="Regenerate the Reference chapter from the registries."
    )
    gen.add_argument(
        "--check", action="store_true", help="Report stale pages without writing (exit 1)."
    )
    gen.set_defaults(_command="manual", _entry=_entry_gen)

    wiki = action.add_parser("wiki", help="Export the manual as a GitHub wiki tree.")
    wiki.add_argument(
        "--out", default="wiki", help="Output directory (a wiki clone; *.md + assets/ replaced)."
    )
    wiki.add_argument("--no-strict", action="store_true", help="Export even when links are broken.")
    wiki.set_defaults(_command="manual", _entry=_entry_wiki)

    serve = action.add_parser("serve", help="Build to a temp dir and serve it locally.")
    serve.add_argument("--port", type=int, default=8765, help="Port to listen on.")
    serve.add_argument("--host", default="127.0.0.1", help="Interface to bind.")
    serve.set_defaults(_command="manual", _entry=_entry_serve)


def _entry_build(args: argparse.Namespace) -> int:
    from ovid2.manual.static_site import build_static_site

    out = Path(args.out).resolve()
    try:
        count = build_static_site(out, strict=not args.no_strict)
    except ValueError as exc:
        print(f"[ovid2] {exc}", file=sys.stderr)
        return 1
    print(f"[ovid2] manual: {count} pages → {out}/index.html")
    return 0


def _entry_wiki(args: argparse.Namespace) -> int:
    from ovid2.manual.wiki import export_wiki

    out = Path(args.out).resolve()
    try:
        result = export_wiki(out, strict=not args.no_strict)
    except ValueError as exc:
        print(f"[ovid2] {exc}", file=sys.stderr)
        return 1
    print(f"[ovid2] manual wiki: {len(result.pages)} pages + Home/_Sidebar/_Footer → {out}")
    return 0


def _entry_check(_args: argparse.Namespace) -> int:
    from ovid2.manual import ManualError, load_manual
    from ovid2.manual.loader import orphan_pages
    from ovid2.manual.reference import write_reference
    from ovid2.manual.render import check_links

    try:
        manual = load_manual()
    except ManualError as exc:
        print(f"[ovid2] manual.yaml / page error: {exc}", file=sys.stderr)
        return 1
    problems = check_links(manual)
    problems += [f"orphan page (not in manual.yaml): {p}" for p in orphan_pages(manual)]
    problems += [
        f"stale generated page (run `ovid2 manual gen-reference`): {p}"
        for p in write_reference(check=True)
    ]
    if problems:
        print("[ovid2] manual check failed:", file=sys.stderr)
        for line in problems:
            print(f"  - {line}", file=sys.stderr)
        return 1
    print(f"[ovid2] manual OK: {len(manual.chapters)} chapters, {len(manual.pages)} pages.")
    return 0


def _entry_gen(args: argparse.Namespace) -> int:
    from ovid2.manual.reference import write_reference

    changed = write_reference(check=args.check)
    if args.check:
        if changed:
            print("[ovid2] stale generated pages:\n  " + "\n  ".join(changed), file=sys.stderr)
            return 1
        print("[ovid2] generated reference is up to date.")
        return 0
    print(
        f"[ovid2] regenerated {len(changed)} page(s)."
        + ("" if not changed else "\n  " + "\n  ".join(changed))
    )
    return 0


def _entry_serve(args: argparse.Namespace) -> int:
    import functools
    import http.server

    from ovid2.manual.static_site import build_static_site

    with tempfile.TemporaryDirectory(prefix="ovid2-manual-") as tmp:
        out = Path(tmp) / "site"
        build_static_site(out, strict=False)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(out))
        with http.server.ThreadingHTTPServer((args.host, args.port), handler) as httpd:
            print(f"[ovid2] serving the manual at http://{args.host}:{args.port}/ (Ctrl-C to stop)")
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print()
    return 0


if __name__ == "__main__":
    _parser = argparse.ArgumentParser(prog="ovid2")
    register(_parser.add_subparsers(dest="command"))
    _ns = _parser.parse_args()
    raise SystemExit(_ns._entry(_ns))
