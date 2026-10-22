"""Tests for :class:`ovid2.dataspace.path.OdsPath` grammar and behaviour."""

from __future__ import annotations

import pytest

from ovid2.dataspace.path import OdsPath, OdsPathError


class TestOdsPathParse:
    """Construction from a POSIX-style string."""

    def test_root(self) -> None:
        p = OdsPath.parse("/")
        assert p.is_root
        assert p.depth == 0
        assert p.pipeline is None
        assert p.as_str() == "/"

    def test_pipeline_only(self) -> None:
        p = OdsPath.parse("/demo")
        assert p.depth == 1
        assert p.pipeline == "demo"
        assert p.batch_id is None
        assert p.table is None

    def test_pipeline_batch(self) -> None:
        p = OdsPath.parse("/demo/20260316")
        assert p.depth == 2
        assert p.batch_id == "20260316"

    def test_leaf(self) -> None:
        p = OdsPath.parse("/demo/20260316/DEMOGRAPHIC")
        assert p.is_leaf
        assert p.depth == 3
        assert p.table == "DEMOGRAPHIC"

    def test_trailing_slash_stripped(self) -> None:
        assert OdsPath.parse("/demo/") == OdsPath.parse("/demo")
        assert OdsPath.parse("/demo/20260316/") == OdsPath.parse("/demo/20260316")

    def test_case_preserved_but_equal(self) -> None:
        a = OdsPath.parse("/DEMO/20260316/Demographic")
        b = OdsPath.parse("/demo/20260316/DEMOGRAPHIC")
        assert a == b
        assert hash(a) == hash(b)
        # Case is preserved in storage even though equality is case-insensitive.
        assert a.pipeline == "DEMO"
        assert a.table == "Demographic"

    def test_dots_in_component_allowed(self) -> None:
        # File-like names with a single dot are legitimate.
        p = OdsPath.parse("/demo/20260316/DEMOGRAPHIC.parquet")
        assert p.table == "DEMOGRAPHIC.parquet"

    def test_underscore_and_hyphen(self) -> None:
        p = OdsPath.parse("/nursing_site/20260316/some-table")
        assert p.pipeline == "nursing_site"
        assert p.table == "some-table"


class TestOdsPathRejections:
    """Grammar violations must raise :class:`OdsPathError`."""

    @pytest.mark.parametrize(
        "bad",
        [
            "demo",  # not absolute
            "",  # empty
            "/demo/../foo",  # parent traversal
            "/demo/./foo",  # current dir
            "/demo//20260316",  # empty component
            "/demo@disc",  # reserved
            "/demo?dim=disc",  # query char
            "/demo#frag",  # fragment char
            "/demo/*/DEMO",  # wildcard
            "/demo/20260316/DEMO/columns",  # depth-4 rejected
        ],
    )
    def test_rejected(self, bad: str) -> None:
        with pytest.raises(OdsPathError):
            OdsPath.parse(bad)

    def test_non_string(self) -> None:
        with pytest.raises(OdsPathError):
            OdsPath.parse(42)  # type: ignore[arg-type]


class TestOdsPathNavigation:
    """``parent`` / ``child`` / ``as_str`` behaviour."""

    def test_parent_of_root_is_root(self) -> None:
        root = OdsPath()
        assert root.parent() == root

    def test_parent_walk(self) -> None:
        leaf = OdsPath.parse("/demo/20260316/DEMO")
        assert leaf.parent() == OdsPath.parse("/demo/20260316")
        assert leaf.parent().parent() == OdsPath.parse("/demo")
        assert leaf.parent().parent().parent().is_root

    def test_child_from_root(self) -> None:
        assert OdsPath().child("demo") == OdsPath.parse("/demo")

    def test_child_leaf_raises(self) -> None:
        leaf = OdsPath.parse("/demo/20260316/DEMO")
        with pytest.raises(OdsPathError):
            leaf.child("columns")

    def test_child_validates_component(self) -> None:
        with pytest.raises(OdsPathError):
            OdsPath().child("../etc")


class TestOdsPathHashing:
    """Frozen dataclass — must work as dict key / set member."""

    def test_set_membership(self) -> None:
        s = {OdsPath.parse("/demo/20260316/DEMO")}
        assert OdsPath.parse("/DEMO/20260316/demo") in s

    def test_dict_key(self) -> None:
        d = {OdsPath.parse("/demo"): 1}
        assert d[OdsPath.parse("/DEMO")] == 1
