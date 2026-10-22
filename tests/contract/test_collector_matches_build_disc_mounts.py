"""Contract: the driver-based collector matches the legacy disc-mount path.

The driver refactor introduces
:func:`ovid2.drivers.collector.collect_for_addresses` as the
successor to :func:`ovid2.connections.disc_mounts.build_disc_mounts`.
Until native driver ports (D3.<type>) land, the two must produce
byte-identical mount lists for every disc-address input shape:

- W7 shape (``{connection: X, location: {…}}`` with X.type == 'disc').
- Legacy shape (``{src_name: 'disc', src_args: {path: '/x'}}``).
- Mixed inputs, dedup, explicit-container-path collisions.

This test lets us confidently swap ``build_disc_mounts`` callers over
to ``collect_for_addresses`` in future PRs without regressions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv(
        "OVID2_CONFIGS_DIR", str(xdg / "ovid2" / "configs"),
    )
    monkeypatch.setenv(
        "OVID2_CONNECTIONS_DIR", str(xdg / "ovid2" / "connections"),
    )
    (xdg / "ovid2" / "configs").mkdir(parents=True)
    (xdg / "ovid2" / "connections").mkdir(parents=True)
    return xdg


def _write_conn(xdg: Path, name: str, payload: dict) -> None:
    (xdg / "ovid2" / "connections" / f"{name}.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


def _write_addr(xdg: Path, name: str, payload: dict) -> None:
    (xdg / "ovid2" / "configs" / f"{name}.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


def _to_triples(mounts) -> set[tuple[str, str, str]]:
    """Normalise both collectors' outputs to a common comparable set."""
    triples = set()
    for m in mounts:
        # ``ExtraMount`` from ``ovid2.tools.extra_mounts`` has (host, container, mode).
        # ``ExtraMount`` from ``ovid2.drivers.container`` has (host_path, container_path, mode).
        host = getattr(m, "host", None) or getattr(m, "host_path", "")
        container = getattr(m, "container", None) or getattr(m, "container_path", "")
        mode = getattr(m, "mode", "rw")
        triples.add((host, container, mode))
    return triples


def test_w7_disc_produces_matching_mounts(
    isolated_config: Path, tmp_path: Path,
) -> None:
    """W7-shape disc addresses match under both collectors."""
    from ovid2.connections.disc_mounts import build_disc_mounts
    from ovid2.drivers.collector import collect_for_addresses

    host = tmp_path / "shared"
    host.mkdir()
    _write_conn(isolated_config, "d_conn", {
        "type": "disc",
        "args": {"host_path": str(host), "mount_mode": "rw"},
    })
    _write_addr(isolated_config, "a", {
        "connection": "d_conn", "location": {"path": "sub"},
    })

    legacy = _to_triples(build_disc_mounts(["a"]))
    driver = _to_triples(collect_for_addresses(["a"]).mounts)
    assert legacy == driver


def test_legacy_disc_produces_matching_mounts(
    isolated_config: Path, tmp_path: Path,
) -> None:
    """Legacy-shape disc addresses match under both collectors."""
    from ovid2.connections.disc_mounts import build_disc_mounts
    from ovid2.drivers.collector import collect_for_addresses

    host = tmp_path / "legacy"
    host.mkdir()
    _write_addr(isolated_config, "legacy_addr", {
        "src_name": "disc",
        "src_args": {"path": str(host)},
    })

    legacy = _to_triples(build_disc_mounts(["legacy_addr"]))
    driver = _to_triples(collect_for_addresses(["legacy_addr"]).mounts)
    assert legacy == driver


def test_explicit_mount_override_matches(
    isolated_config: Path, tmp_path: Path,
) -> None:
    """Both collectors drop the auto-mount when the container path collides."""
    from ovid2.connections.disc_mounts import build_disc_mounts
    from ovid2.drivers.collector import collect_for_addresses

    host = tmp_path / "d"
    host.mkdir()
    _write_conn(isolated_config, "d_conn", {
        "type": "disc",
        "args": {"host_path": str(host)},
    })
    _write_addr(isolated_config, "d_addr", {
        "connection": "d_conn", "location": {},
    })

    legacy = _to_triples(build_disc_mounts(
        ["d_addr"], explicit_container_paths=[str(host)],
    ))
    driver = _to_triples(collect_for_addresses(
        ["d_addr"], explicit_container_paths=[str(host)],
    ).mounts)
    assert legacy == driver == set()


def test_dedup_matches(isolated_config: Path, tmp_path: Path) -> None:
    """Two addresses onto the same host path dedup in both collectors."""
    from ovid2.connections.disc_mounts import build_disc_mounts
    from ovid2.drivers.collector import collect_for_addresses

    host = tmp_path / "shared"
    host.mkdir()
    _write_conn(isolated_config, "d_conn", {
        "type": "disc",
        "args": {"host_path": str(host)},
    })
    _write_addr(isolated_config, "a1", {"connection": "d_conn", "location": {}})
    _write_addr(isolated_config, "a2", {"connection": "d_conn", "location": {}})

    legacy = _to_triples(build_disc_mounts(["a1", "a2"]))
    driver = _to_triples(collect_for_addresses(["a1", "a2"]).mounts)
    assert legacy == driver
    assert len(legacy) == 1
