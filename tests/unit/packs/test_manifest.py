"""Unit tests — ``ovid2.packs.manifest`` (schema validation + version constraints)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ovid2.packs.manifest import (
    PackManifestError,
    load_manifest,
    parse_manifest,
    version_satisfies,
)

pytestmark = pytest.mark.unit

_MIN = {"v": 1, "id": "acme", "version": "1.0", "label": "Acme"}


def test_parse_minimal_manifest() -> None:
    m = parse_manifest(dict(_MIN))
    assert (m.id, m.version, m.label) == ("acme", "1.0", "Acme")
    assert m.cdms == () and m.parts == () and m.fixers == {} and m.members == {}
    assert m.as_dict()["provides"] == {}


def test_parse_reads_provides() -> None:
    m = parse_manifest(
        _MIN
        | {
            "provides": {
                "cdms": ["acme", "acme_v2"],
                "checks_dir": "checks",
                "parts": [{"id": "acme_safety", "label": "Safety"}],
                "fixers": {"acme_fix": {"summary": "Fix it.", "scope": "acme"}},
            }
        }
    )
    assert m.cdms == ("acme", "acme_v2")
    assert m.dir("checks_dir") == "checks" and m.dir("packages_dir") is None
    assert m.parts == (("acme_safety", "Safety"),)
    assert m.fixers["acme_fix"]["scope"] == "acme"


@pytest.mark.parametrize(
    ("patch", "where"),
    [
        ({"id": "Bad-Id"}, "id"),
        ({"v": 2}, "v"),
        ({"surprise": 1}, "(root)"),
        ({"provides": {"checks_dir": "../escape"}}, "provides/checks_dir"),
        ({"provides": {"checks_dir": "/abs"}}, "provides/checks_dir"),
        ({"provides": {"unknown_key": "x"}}, "provides"),
        ({"provides": {"fixers": {"acme_fix": {}}}}, "provides/fixers/acme_fix"),
    ],
)
def test_parse_rejects_invalid(patch: dict[str, object], where: str) -> None:
    with pytest.raises(PackManifestError, match=where.replace("(", r"\(").replace(")", r"\)")):
        parse_manifest(_MIN | patch)


def test_parse_rejects_non_mapping() -> None:
    with pytest.raises(PackManifestError, match="mapping"):
        parse_manifest(["not", "a", "dict"])


def test_load_manifest_missing_and_bad_yaml(tmp_path: Path) -> None:
    with pytest.raises(PackManifestError, match=r"no pack\.yaml"):
        load_manifest(tmp_path)
    (tmp_path / "pack.yaml").write_text("id: [unclosed\n", encoding="utf-8")
    with pytest.raises(PackManifestError):
        load_manifest(tmp_path)


@pytest.mark.parametrize(
    ("version", "constraint", "ok"),
    [
        ("0.1.0", "", True),
        ("0.1.0", ">=0.1", True),
        ("0.1.0", ">=0.2", False),
        ("0.1.0", ">=0.1,<1.0", True),
        ("1.0.0", ">=0.1,<1.0", False),
        ("0.1.0", "==0.1", True),  # trailing zeros are insignificant
        ("0.1.0", "!=0.1.0", False),
        ("0.2.0rc1", ">0.1", True),  # pre-release tag ignored
        ("0.1.0", "<=0.1.0", True),
    ],
)
def test_version_satisfies(version: str, constraint: str, ok: bool) -> None:
    assert version_satisfies(version, constraint) is ok


def test_version_constraint_unparseable() -> None:
    with pytest.raises(PackManifestError, match="cannot parse"):
        version_satisfies("0.1.0", "~=0.1")
