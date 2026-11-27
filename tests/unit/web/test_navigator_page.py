"""Unit tests — the /navigator page shell and its cross-links."""

from __future__ import annotations

import json
import re

import pytest

pytestmark = pytest.mark.unit


def test_navigator_page_renders(web_client) -> None:  # type: ignore[no-untyped-def]
    """``/navigator`` is the Atlas; the historical deep link still lands on it."""
    r = web_client.get(
        "/navigator?p=example_pcornet&t=demographic&f=SEX&group_by=profile&include_unused=true"
    )
    assert r.status_code == 200, r.text
    body = r.text
    assert 'id="atlas"' in body
    assert 'data-initial-p="example_pcornet"' in body
    assert 'data-initial-t="demographic"' in body
    assert 'data-initial-f="SEX"' in body
    island = re.search(
        r'<script type="application/json" id="atlas-boot">(.*?)</script>', body, re.S
    )
    assert island
    graph = json.loads(island.group(1))["graph"]
    assert graph["layers"] == ["storage", "compute", "algorithm"]
    assert 'data-nav="navigator"' in body


def test_navigator_bad_group_by_is_ignored(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/navigator?group_by=nonsense")
    assert r.status_code == 200
    assert 'id="atlas"' in r.text


def test_builder_and_package_pages_link_to_navigator(web_client) -> None:  # type: ignore[no-untyped-def]
    r = web_client.get("/pipeline-packages/example_pcornet/builder")
    assert r.status_code == 200
    assert "/navigator?p=example_pcornet" in r.text
    r = web_client.get("/pipeline-packages/example_pcornet")
    assert r.status_code == 200
    assert "/navigator?p=example_pcornet" in r.text
