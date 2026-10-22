"""Shared fixtures for MCP-tier tests.

The MCP server is a thin HTTP client on top of the FastAPI backend, so
every unit test wants two things:

1. **An isolated XDG tree** so the on-disk artefacts each request
   creates (SQLite ``scheduler.db``, address configs, secrets) never
   collide with other tests or the host's real install. The web-tier
   ``isolated_xdg`` fixture already does this — we re-use it via
   :mod:`tests.unit.web.conftest`.
2. **A live FastAPI app + in-process client** so tools can be driven
   against a real backend without spinning up uvicorn.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect every XDG env var into ``tmp_path``.

    Mirrors :func:`tests.unit.web.conftest.isolated_xdg` — copied here
    (rather than imported) so the MCP tests do not depend on the web
    ``conftest`` being collected first.
    """
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_RUNTIME_DIR", "runtime"),
    ):
        target = tmp_path / sub
        target.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(var, str(target))
    monkeypatch.setenv("OVID2_WORK_DIR", str(tmp_path / "runtime" / "ovid2"))
    monkeypatch.setenv("OVID2_SCHEDULER_DB", str(tmp_path / "state" / "ovid2" / "scheduler.db"))
    monkeypatch.setenv("OVID2_CONFIGS_DIR", str(tmp_path / "config" / "ovid2" / "configs"))
    (tmp_path / "config" / "ovid2" / "configs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "ovid2").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime" / "ovid2").mkdir(parents=True, exist_ok=True)

    import ovid2.config.settings as _settings

    importlib.reload(_settings)
    yield tmp_path


@pytest.fixture()
def web_app(isolated_xdg: Path):
    """Return a freshly-constructed FastAPI ``app`` bound to the isolated XDG."""
    pytest.importorskip("fastapi")
    import ovid2.web.app as _app_module
    import ovid2.web.settings as _web_settings

    importlib.reload(_web_settings)
    importlib.reload(_app_module)
    return _app_module.app


@pytest.fixture()
def mcp_client(web_app):
    """Return a :class:`WebApiClient` bound to the in-process FastAPI app."""
    pytest.importorskip("httpx")
    from ovid2.mcp.client import build_client_for_asgi

    return build_client_for_asgi(web_app)


@pytest.fixture()
def mcp_bundle(mcp_client, web_app):
    """Return a :class:`ServerBundle` bound to the in-process client."""
    pytest.importorskip("mcp")
    from ovid2.mcp.server import build_server

    bundle = build_server(client=mcp_client)
    yield bundle


@pytest.fixture()
def sample_address(isolated_xdg: Path) -> Path:
    """Drop a sample disc address config on the search path."""
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    path = configs / "test_disc.json"
    path.write_text(
        '{"src_name": "disc", "src_args": {"path": "/tmp/example"}}',
        encoding="utf-8",
    )
    return path
