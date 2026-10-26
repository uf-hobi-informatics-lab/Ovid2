"""Test helper: stub the web tier's overwrite pre-flight.

Every submit / rerun / launch asks ``output_probe_svc.existing_output_tables``
whether the (exact-location) output already holds tables — in production
through an ``ovid2 tools probe-output`` executor subprocess. Unit tests stub
it so they neither spawn subprocesses nor depend on broker plumbing.
Overwrite-specific tests call :func:`set_output_tables`.
"""

from __future__ import annotations

import pytest


def stub_output_probe(
    monkeypatch: pytest.MonkeyPatch,
    tables: list[str] | None = None,
) -> None:
    """Make the overwrite pre-flight report ``tables`` (``None`` = unknown)."""
    from ovid2.web.services import output_probe_svc

    def _probe(_address: str) -> output_probe_svc.OutputProbe:
        if tables is None:
            return output_probe_svc.OutputProbe(known=False, tables=[], message="stubbed")
        return output_probe_svc.OutputProbe(known=True, tables=list(tables))

    monkeypatch.setattr(output_probe_svc, "existing_output_tables", _probe)


set_output_tables = stub_output_probe
