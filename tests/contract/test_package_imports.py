"""Contract: the ``ovid2`` package imports cleanly and exposes ``__version__``.

A trivial safety net that catches broken ``__init__.py`` files early.
"""

from __future__ import annotations

import pytest


@pytest.mark.contract
def test_package_imports() -> None:
    """The top-level package imports and has a ``__version__`` string."""
    import ovid2

    assert isinstance(ovid2.__version__, str)
    assert ovid2.__version__


@pytest.mark.contract
def test_cli_module_importable() -> None:
    """The CLI entry point is importable without side effects."""
    from ovid2.cli import __main__ as cli_main

    parser = cli_main._build_parser()
    args = parser.parse_args([])
    assert not getattr(args, "_command", None)
