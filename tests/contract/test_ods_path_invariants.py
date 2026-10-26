"""Contract: :class:`OdsPath` grammar guardrails.

Every data space is the same — a tree of plain directories rooted at a
storage account: ``/<connection>/<path…>``. The invariants below are
load-bearing for the resolver, the web routes, and the MCP tools:

- There is no ``pipeline`` / ``batch_id`` level — any depth of folders
  (up to a sanity ceiling) is a valid location.
- Components never contain ``..`` / ``.`` / wildcards / reserved
  characters (``@``, ``#``, ``?``, ``*``).
- Trailing slashes are stripped so equality survives slash noise.
- Comparison is case-insensitive.
"""

from __future__ import annotations

import pytest

from ovid2.dataspace.path import OdsPath, OdsPathError


class TestGrammarGuardrails:
    def test_first_segment_is_the_storage_account(self) -> None:
        p = OdsPath.parse("/inbox/landing/2026_03")
        assert p.connection == "inbox"
        assert p.location == "landing/2026_03"
        assert p.batch_id() == "inbox/landing/2026_03"

    def test_deep_folders_allowed(self) -> None:
        assert OdsPath.parse("/a/b/c/d/e/f").depth == 6

    def test_depth_ceiling(self) -> None:
        with pytest.raises(OdsPathError):
            OdsPath.parse("/" + "/".join(["x"] * (OdsPath.MAX_DEPTH + 1)))

    @pytest.mark.parametrize(
        "bad",
        [
            "/a/../b",
            "/a/./b",
            "/*",
            "/a/@z",
            "/a/#z",
            "/a/?z",
        ],
    )
    def test_reserved_characters(self, bad: str) -> None:
        with pytest.raises(OdsPathError):
            OdsPath.parse(bad)

    def test_absolute_only(self) -> None:
        with pytest.raises(OdsPathError):
            OdsPath.parse("demo/20260316")

    def test_trailing_slash_stripped(self) -> None:
        assert OdsPath.parse("/demo/").as_str() == "/demo"

    def test_case_insensitive_equality(self) -> None:
        assert OdsPath.parse("/DEMO/20260316/DEMO") == OdsPath.parse("/demo/20260316/demo")

    def test_no_pipeline_level(self) -> None:
        assert not hasattr(OdsPath, "pipeline")
