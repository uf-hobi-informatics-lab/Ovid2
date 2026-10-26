"""Tests for :class:`ovid2.dataspace.path.OdsPath` (``/<connection>/<path…>``)."""

from __future__ import annotations

import pytest

from ovid2.dataspace.path import OdsPath, OdsPathError


class TestOdsPathParse:
    """Construction from a POSIX-style string."""

    def test_root(self) -> None:
        p = OdsPath.parse("/")
        assert p.is_root
        assert p.depth == 0
        assert p.connection is None
        assert p.as_str() == "/"

    def test_account_only(self) -> None:
        p = OdsPath.parse("/inbox")
        assert p.depth == 1
        assert p.connection == "inbox"
        assert p.location == ""
        assert p.batch_id() == "inbox"

    def test_folder_path(self) -> None:
        p = OdsPath.parse("/inbox/landing/2026_03")
        assert p.depth == 3
        assert p.connection == "inbox"
        assert p.location == "landing/2026_03"
        assert p.name == "2026_03"
        assert p.batch_id() == "inbox/landing/2026_03"

    def test_warehouse_schema(self) -> None:
        p = OdsPath.parse("/sf_prod/CHAR_DB/SITE_X")
        assert p.location == "CHAR_DB/SITE_X"

    def test_trailing_slash_stripped(self) -> None:
        assert OdsPath.parse("/demo/") == OdsPath.parse("/demo")

    def test_case_preserved_but_equal(self) -> None:
        a = OdsPath.parse("/DEMO/20260316/Demographic")
        b = OdsPath.parse("/demo/20260316/DEMOGRAPHIC")
        assert a == b
        assert hash(a) == hash(b)
        assert a.connection == "DEMO"
        assert a.name == "Demographic"

    def test_dots_dollar_underscore_hyphen(self) -> None:
        p = OdsPath.parse("/s3_x/some-dir/DEMOGRAPHIC.parquet/T$1")
        assert p.parts == ("some-dir", "DEMOGRAPHIC.parquet", "T$1")


class TestOdsPathRejects:
    @pytest.mark.parametrize(
        "bad",
        ["", "demo", "/a//b", "/a/../b", "/a/./b", "/a/*", "/a/@b", "/a/#b", "/a/?b"],
    )
    def test_rejected(self, bad: str) -> None:
        with pytest.raises(OdsPathError):
            OdsPath.parse(bad)

    def test_non_string(self) -> None:
        with pytest.raises(OdsPathError):
            OdsPath.parse(123)  # type: ignore[arg-type]


class TestOdsPathNavigation:
    def test_parent_of_root_is_root(self) -> None:
        assert OdsPath().parent() == OdsPath()

    def test_parent_walk(self) -> None:
        p = OdsPath.parse("/a/b/c")
        assert p.parent().as_str() == "/a/b"
        assert p.parent().parent().as_str() == "/a"
        assert p.parent().parent().parent().is_root

    def test_child(self) -> None:
        assert OdsPath().child("acct").as_str() == "/acct"
        assert OdsPath.parse("/acct").child("x").child("y").as_str() == "/acct/x/y"

    def test_child_validates_component(self) -> None:
        with pytest.raises(OdsPathError):
            OdsPath.parse("/acct").child("..")


class TestOdsPathHashing:
    def test_set_membership(self) -> None:
        assert OdsPath.parse("/A/b") in {OdsPath.parse("/a/B")}
