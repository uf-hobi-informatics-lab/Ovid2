"""Unit tests for :mod:`ovid2.drivers.container`."""

from __future__ import annotations

from ovid2.drivers.container import (
    EnvOverlay,
    ExtraMount,
    JavaPackage,
    MergedRequirements,
    collect_requirements,
)


class TestExtraMount:
    def test_defaults_to_rw(self) -> None:
        m = ExtraMount(host_path="/h", container_path="/c")
        assert m.mode == "rw"

    def test_is_hashable_frozen(self) -> None:
        m1 = ExtraMount(host_path="/h", container_path="/c")
        m2 = ExtraMount(host_path="/h", container_path="/c")
        assert m1 == m2
        assert {m1, m2} == {m1}


class TestJavaPackage:
    def test_excludes_default_empty(self) -> None:
        p = JavaPackage(coordinate="org.apache.hadoop:hadoop-aws:3.3.2")
        assert p.excludes == ()

    def test_equality(self) -> None:
        p1 = JavaPackage(coordinate="a:b:1")
        p2 = JavaPackage(coordinate="a:b:1")
        assert p1 == p2


class TestEnvOverlay:
    def test_holds_name_and_value(self) -> None:
        e = EnvOverlay(name="FOO", value="bar")
        assert e.name == "FOO"
        assert e.value == "bar"


class TestCollectRequirements:
    def test_empty_input_returns_empty_buckets(self) -> None:
        got = collect_requirements(())
        assert got == MergedRequirements()

    def test_single_mount(self) -> None:
        m = ExtraMount(host_path="/h", container_path="/c")
        got = collect_requirements((m,))
        assert got.mounts == (m,)
        assert got.java_packages == ()
        assert got.env_overlays == ()

    def test_dedup_identical_java_packages(self) -> None:
        p = JavaPackage(coordinate="a:b:1")
        got = collect_requirements((p, p, p))
        assert got.java_packages == (p,)

    def test_dedup_identical_mounts(self) -> None:
        m = ExtraMount(host_path="/h", container_path="/c", mode="ro")
        got = collect_requirements((m, m))
        assert got.mounts == (m,)

    def test_different_modes_are_distinct(self) -> None:
        m1 = ExtraMount(host_path="/h", container_path="/c", mode="rw")
        m2 = ExtraMount(host_path="/h", container_path="/c", mode="ro")
        got = collect_requirements((m1, m2))
        assert got.mounts == (m1, m2)

    def test_multiple_types_bucketed_separately(self) -> None:
        m = ExtraMount(host_path="/h", container_path="/c")
        p = JavaPackage(coordinate="a:b:1")
        e = EnvOverlay(name="FOO", value="bar")
        got = collect_requirements((m, p, e))
        assert got.mounts == (m,)
        assert got.java_packages == (p,)
        assert got.env_overlays == (e,)

    def test_accepts_list_input(self) -> None:
        m = ExtraMount(host_path="/h", container_path="/c")
        got = collect_requirements([m])
        assert got.mounts == (m,)
