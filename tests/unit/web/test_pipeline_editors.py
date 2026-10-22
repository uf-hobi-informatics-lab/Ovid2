"""Phase-C re-homed pipeline-package editors under /pipeline-packages/*.

The package *roster* + editors (formatters / mappers / file preview)
live under ``/pipeline-packages/*``; the ``/pipelines/*`` namespace is
reserved for runnable pipeline *configs*. There is no legacy redirect
layer — the canonical editor URLs are served directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_VALID_FORMATTER = (
    "v: 1\n"
    "formatter: demographic\n"
    "adapter: source_passthrough\n"
    'sources:\n  demographic:\n    glob: "*Demographic*"\n'
    "projections:\n  PATID: patid\n"
)

_VALID_MAPPER = "v: 1\ntable: demographic\nops: []\n"


@pytest.fixture()
def pipeline_pkg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_xdg: Path) -> str:
    """Register a minimal pipeline package (ovid2_pipelines.tp_edit)."""
    root = tmp_path / "pipelines_root"
    pkg = root / "ovid2_pipelines" / "tp_edit"
    (pkg / "formatters").mkdir(parents=True)
    (pkg / "mappers").mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "pipeline_settings.py").write_text(
        "mappers_type = 'pcornet_mappers'\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(root))
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]
    yield "tp_edit"
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]


# --------------------------------------------------------------------------- #
# Editor pages render under /pipeline-packages/{name}/...
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_pipeline_hub_renders(web_client: object, pipeline_pkg: str) -> None:
    r = web_client.get(f"/pipeline-packages/{pipeline_pkg}")  # type: ignore[attr-defined]
    assert r.status_code == 200
    assert pipeline_pkg in r.text
    assert "Pipeline:" in r.text


@pytest.mark.unit
def test_pipeline_meta_roster_renders(web_client: object, pipeline_pkg: str) -> None:
    r = web_client.get("/pipeline-packages")  # type: ignore[attr-defined]
    assert r.status_code == 200
    assert pipeline_pkg in r.text


@pytest.mark.unit
def test_formatter_new_page_renders(web_client: object, pipeline_pkg: str) -> None:
    r = web_client.get(f"/pipeline-packages/{pipeline_pkg}/formatters/new")  # type: ignore[attr-defined]
    assert r.status_code == 200


@pytest.mark.unit
def test_mapper_new_page_renders(web_client: object, pipeline_pkg: str) -> None:
    r = web_client.get(f"/pipeline-packages/{pipeline_pkg}/mappers/new")  # type: ignore[attr-defined]
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# Save + view + delete round-trips under /pipeline-packages/{name}/...
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_formatter_create_view_delete(web_client: object, pipeline_pkg: str) -> None:
    create = web_client.post(  # type: ignore[attr-defined]
        f"/pipeline-packages/{pipeline_pkg}/formatters/new",
        data={"table": "demographic", "raw_text": _VALID_FORMATTER},
        follow_redirects=False,
    )
    assert create.status_code == 303
    assert create.headers["location"] == (
        f"/pipeline-packages/{pipeline_pkg}/formatters/demographic"
    )

    view = web_client.get(  # type: ignore[attr-defined]
        f"/pipeline-packages/{pipeline_pkg}/formatters/demographic"
    )
    assert view.status_code == 200

    delete = web_client.post(  # type: ignore[attr-defined]
        f"/pipeline-packages/{pipeline_pkg}/formatters/demographic/delete",
        follow_redirects=False,
    )
    assert delete.status_code == 303
    assert delete.headers["location"] == f"/pipeline-packages/{pipeline_pkg}"


@pytest.mark.unit
def test_mapper_create_view_delete(web_client: object, pipeline_pkg: str) -> None:
    create = web_client.post(  # type: ignore[attr-defined]
        f"/pipeline-packages/{pipeline_pkg}/mappers/new",
        data={"table": "demographic", "raw_text": _VALID_MAPPER},
        follow_redirects=False,
    )
    assert create.status_code == 303
    assert create.headers["location"] == (f"/pipeline-packages/{pipeline_pkg}/mappers/demographic")
    view = web_client.get(  # type: ignore[attr-defined]
        f"/pipeline-packages/{pipeline_pkg}/mappers/demographic"
    )
    assert view.status_code == 200
    delete = web_client.post(  # type: ignore[attr-defined]
        f"/pipeline-packages/{pipeline_pkg}/mappers/demographic/delete",
        follow_redirects=False,
    )
    assert delete.status_code == 303
    assert delete.headers["location"] == f"/pipeline-packages/{pipeline_pkg}"


# --------------------------------------------------------------------------- #
# Canonical /pipeline-packages/* editor URLs serve directly (there is no
# legacy back-compat redirect layer).
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_pipeline_packages_root_renders(web_client: object, pipeline_pkg: str) -> None:
    r = web_client.get("/pipeline-packages", follow_redirects=False)  # type: ignore[attr-defined]
    assert r.status_code == 200


@pytest.mark.unit
def test_pipeline_package_detail_renders(web_client: object, pipeline_pkg: str) -> None:
    r = web_client.get(  # type: ignore[attr-defined]
        f"/pipeline-packages/{pipeline_pkg}", follow_redirects=False
    )
    assert r.status_code == 200


@pytest.mark.unit
def test_pipeline_package_file_renders(web_client: object, pipeline_pkg: str) -> None:
    r = web_client.get(  # type: ignore[attr-defined]
        f"/pipeline-packages/{pipeline_pkg}/file?relative_path=pipeline_settings.py",
        follow_redirects=False,
    )
    assert r.status_code == 200


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
