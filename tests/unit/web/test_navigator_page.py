"""Unit tests — the /navigator page shell and its cross-links."""

from __future__ import annotations

import importlib.util
import json
import re

import pytest

pytestmark = pytest.mark.unit


def test_navigator_page_renders(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/navigator?p=example_pcornet&t=demographic&f=SEX&group_by=profile")
    assert r.status_code == 200, r.text
    body = r.text
    assert 'id="navigator"' in body
    assert 'data-initial-p="example_pcornet"' in body
    assert 'data-initial-t="demographic"' in body
    assert 'data-initial-f="SEX"' in body
    assert 'data-group-by="profile"' in body
    assert "/static/navigator.js" in body
    island = re.search(
        r'<script type="application/json" id="nv-tree-data">(.*?)</script>', body, re.S
    )
    assert island
    tree = json.loads(island.group(1))
    assert tree["group_by"] == "profile"
    labels = {g["label"] for g in tree["groups"]}
    assert "default" in labels
    # Legacy-parity ports are site packages (absent from the public mirror).
    if importlib.util.find_spec("ovid2_pipelines.site") is not None:
        assert "legacy_ovid" in labels
    # U2: the group-by is a select over the registry's axes.
    assert re.search(r'<select data-nv-group[^>]*>.*?value="cdm_profile"', body, re.S)
    # Nav entry is present and active.
    assert 'data-nav="navigator"' in body


def test_navigator_bad_group_by_falls_back(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/navigator?group_by=nonsense")
    assert r.status_code == 200
    assert 'data-group-by="cdm"' in r.text


def test_builder_and_package_pages_link_to_navigator(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/pipeline-packages/example_pcornet/builder")
    assert r.status_code == 200
    assert "/navigator?p=example_pcornet" in r.text
    r = web_client.get("/pipeline-packages/example_pcornet")
    assert r.status_code == 200
    assert "/navigator?p=example_pcornet" in r.text
