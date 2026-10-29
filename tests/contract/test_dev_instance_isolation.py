"""Contract: an ``ovid2 dev`` instance's state never escapes its root.

Two guarantees:

1. Every state location the web tier / executor resolves (scheduler DB,
   settings, secrets, address configs, connections, engines, LLMs,
   pipelines, logs, exports, archive, work dir) lands inside the
   instance root when the process runs with
   :func:`ovid2.cli.dev.instance_env`.
2. Every ``OVID2_*_DIR`` / ``*_FILE`` / ``*_DB`` env override that exists
   in ``src/ovid2`` is classified: either stripped from a dev instance's
   environment (``_LEAKY_ENV`` — it would point at someone else's state)
   or explicitly listed as intentionally shared read-only input. A new
   override added without a decision fails here.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from ovid2.cli import dev

#: Overrides deliberately inherited by dev instances: shared, read-mostly
#: reference data / inputs, not per-instance state.
_SHARED_OK: frozenset[str] = frozenset(
    {"OVID2_MATERIAL_DIR", "OVID2_DATA_DIR", "OVID2_CREDENTIALS_DIR"}
)

_SRC = Path(__file__).resolve().parents[2] / "src" / "ovid2"


@pytest.mark.contract
def test_every_path_override_is_classified() -> None:
    pattern = re.compile(r"OVID2_[A-Z0-9_]+?(?:_DIR|_FILE|_DB)\b")
    found: set[str] = set()
    for py in _SRC.rglob("*.py"):
        found.update(pattern.findall(py.read_text(encoding="utf-8")))
    unclassified = sorted(found - set(dev._LEAKY_ENV) - _SHARED_OK)
    assert not unclassified, (
        "New OVID2_* path override(s) must be added to ovid2.cli.dev._LEAKY_ENV "
        "(per-instance state) or _SHARED_OK in this test (shared input): " + ", ".join(unclassified)
    )


@pytest.mark.contract
def test_resolved_state_paths_stay_inside_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "inst"
    env = dev.instance_env(
        root, name="a", host="127.0.0.1", port=8100, mcp_port=8101, image="ovid2:dev-t", base={}
    )
    for key in dev._LEAKY_ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(root.parent)

    import ovid2.config.settings as core_settings
    from ovid2.config import connections, engines, llms
    from ovid2.config import paths as xdg
    from ovid2.display.renderers import json_file
    from ovid2.web import settings as web_settings
    from ovid2.web.services import settings_svc

    core_settings = importlib.reload(core_settings)

    resolved = {
        "config_dir": xdg.config_dir(),
        "secrets_dir": xdg.secrets_dir(),
        "configs_dir": xdg.configs_dir(),
        "cache_dir": xdg.cache_dir(),
        "state_dir": xdg.state_dir(),
        "runtime_dir": xdg.runtime_dir(),
        "scheduler_db": web_settings.web_db_path(),
        "web_logs": web_settings.web_log_dir(),
        "settings_json": settings_svc.settings_path(),
        "connections": connections.connections_dir(),
        "engines": engines.engines_dir(),
        "llms": llms.llms_dir(),
        "archive": json_file._default_archive_dir(),
        "work_dir": core_settings.WORK_DIR,
    }
    escaped = {k: str(v) for k, v in resolved.items() if not Path(v).is_relative_to(root)}
    assert not escaped, f"dev-instance state escaped {root}: {escaped}"
