"""Contract: :class:`OdsPath` grammar guardrails.

The invariants below are load-bearing for the whole ODS naming
layer — the resolver, the web routes, and the MCP tools all assume:

- Paths never exceed depth 3.
- Path components never contain ``..`` / ``.`` / wildcards / reserved
  characters (``@``, ``#``, ``?``, ``*``).
- Trailing slashes are stripped before storage so equality survives
  slash noise.
- Comparison is case-insensitive so the three-way pipeline-case
  fallback used by :class:`DiscAddress` continues to work.
"""

from __future__ import annotations

import pytest

from ovid2.dataspace.path import OdsPath, OdsPathError


class TestGrammarGuardrails:
    def test_max_depth_is_three(self) -> None:
        assert OdsPath.MAX_DEPTH == 3

    def test_depth_four_rejected(self) -> None:
        with pytest.raises(OdsPathError):
            OdsPath.parse("/a/b/c/d")

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
