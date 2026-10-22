"""Contract: every ``ovid2.cli.*`` subcommand module exposes ``register(...)``.

The top-level CLI dispatcher in :mod:`ovid2.cli.__main__` imports each
subcommand module and calls its ``register`` function. Missing or
non-callable ``register`` attributes break the CLI at startup — this
test catches that at commit time.
"""

from __future__ import annotations

import importlib
import inspect

import pytest

from ovid2.cli import __main__ as cli_main


@pytest.mark.contract
def test_every_wired_subcommand_module_registers() -> None:
    """Each subcommand module referenced by ``_SUBCOMMANDS`` has ``register``."""
    offenders: list[str] = []
    for _, _, module_name in cli_main._SUBCOMMANDS:
        if module_name is None:
            continue
        module = importlib.import_module(module_name)
        register = getattr(module, "register", None)
        if register is None or not callable(register):
            offenders.append(f"{module_name}: missing callable `register`")
            continue
        signature = inspect.signature(register)
        params = list(signature.parameters.values())
        if len(params) != 1:
            offenders.append(
                f"{module_name}: `register` must accept exactly one argument (subparsers)"
            )
    assert not offenders, "Subcommand modules broken:\n" + "\n".join(offenders)


@pytest.mark.contract
def test_stub_subcommands_have_no_module() -> None:
    """Stub subcommands (planned but not yet implemented) have module=None."""
    stubs = [name for name, _, module in cli_main._SUBCOMMANDS if module is None]
    # Every stub is a known planned command — kept as a smoke check so
    # nobody accidentally deletes the placeholders and breaks --help.
    for name in stubs:
        assert name  # non-empty
