"""Unit tests for :mod:`ovid2.drivers.legacy_adapter`."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ovid2.drivers.base import ConnectionFieldSpec, Driver, LocationField, ProbeResult
from ovid2.drivers.container import ExtraMount
from ovid2.drivers.legacy_adapter import build_legacy_driver_class
from ovid2.drivers.registry import DRIVERS, instantiate, list_types, load_class


class TestRegistryPopulation:
    def test_all_legacy_types_registered(self) -> None:
        from ovid2.connections.registry import CONNECTION_TYPE_MAP

        for type_name in CONNECTION_TYPE_MAP:
            assert type_name in DRIVERS, f"Legacy type '{type_name}' missing from DRIVERS."

    def test_loaded_class_is_driver_subclass(self) -> None:
        for type_name in list_types():
            cls = load_class(type_name)
            assert isinstance(cls, type)
            assert issubclass(cls, Driver)


class TestLegacyMetadataBridge:
    @pytest.mark.parametrize(
        "type_name",
        [
            "disc",
            "s3",
            "azure_blob",
            "snowflake",
            "databricks",
            "postgres",
            "mssql",
            "gcs",
        ],
    )
    def test_connection_fields_produces_spec(self, type_name: str) -> None:
        cls = load_class(type_name)
        spec = cls.connection_fields()
        assert isinstance(spec, ConnectionFieldSpec)

    @pytest.mark.parametrize(
        "type_name",
        [
            "disc",
            "s3",
            "azure_blob",
            "snowflake",
            "databricks",
            "postgres",
            "mssql",
            "gcs",
        ],
    )
    def test_location_fields_produces_tuple(self, type_name: str) -> None:
        cls = load_class(type_name)
        fields = cls.location_fields()
        assert isinstance(fields, tuple)
        for f in fields:
            assert isinstance(f, LocationField)

    def test_disc_connection_fields_match_legacy_shape(self) -> None:
        """Round-trip check: adapter output must match ``_FIELD_SPECS``."""
        from ovid2.connections.registry import connection_field_spec

        legacy = connection_field_spec("disc")
        adapter = load_class("disc").connection_fields().to_dict()
        # Note: ``help`` strings must survive verbatim.
        assert [f["name"] for f in adapter["args"]] == [f["name"] for f in legacy["args"]]
        assert adapter["secrets"] == legacy["secrets"]
        assert adapter.get("note") == legacy.get("note")

    def test_snowflake_secret_field_survives(self) -> None:
        """Snowflake has one optional password secret."""
        cls = load_class("snowflake")
        spec = cls.connection_fields()
        assert [f.name for f in spec.secrets] == ["password"]

    def test_azure_note_survives(self) -> None:
        """Azure ships a connector-level note; make sure it round-trips."""
        cls = load_class("azure_blob")
        spec = cls.connection_fields()
        assert spec.note is not None
        assert "managed identity" in spec.note.lower()


class TestLegacyProbeBridge:
    def test_disc_probe_auth_ok_on_readable_dir(self, tmp_path: Path) -> None:
        d = instantiate("disc", args={"host_path": str(tmp_path), "mount_mode": "rw"})
        r = d.probe_auth()
        assert r.ok is True
        assert r.code == "ok"
        assert r.capability == "auth"

    def test_disc_probe_auth_reports_missing_dir(self) -> None:
        d = instantiate("disc", args={"host_path": "/nonexistent-xyz-123", "mount_mode": "rw"})
        r = d.probe_auth()
        assert r.ok is False
        assert r.code == "unreachable"
        assert r.capability == "auth"

    def test_disc_probe_with_no_host_path(self) -> None:
        d = instantiate("disc", args={})
        r = d.probe_auth()
        assert r.ok is False
        assert r.code == "config"

    def test_probe_result_is_typed(self) -> None:
        d = instantiate("disc", args={"host_path": "/tmp"})
        r = d.probe_auth()
        assert isinstance(r, ProbeResult)


class TestLegacyContainerReqs:
    def test_disc_returns_extra_mount(self, tmp_path: Path) -> None:
        d = instantiate("disc", args={"host_path": str(tmp_path), "mount_mode": "rw"})
        reqs = d.container_requirements_for({})
        assert len(reqs) == 1
        mount = reqs[0]
        assert isinstance(mount, ExtraMount)
        assert mount.host_path == str(tmp_path)
        assert mount.container_path == str(tmp_path)
        assert mount.mode == "rw"

    def test_disc_with_container_path_override(self, tmp_path: Path) -> None:
        d = instantiate(
            "disc",
            args={
                "host_path": str(tmp_path),
                "container_path": "/inside",
                "mount_mode": "ro",
            },
        )
        reqs = d.container_requirements_for({})
        assert reqs[0].host_path == str(tmp_path)
        assert reqs[0].container_path == "/inside"
        assert reqs[0].mode == "ro"

    def test_disc_with_empty_host_path_returns_empty(self) -> None:
        d = instantiate("disc", args={})
        reqs = d.container_requirements_for({})
        assert reqs == ()

    def test_non_disc_type_returns_empty(self) -> None:
        d = instantiate("s3", args={"region": "us-east-1"})
        reqs = d.container_requirements_for({})
        assert reqs == ()


class TestLegacySrcArgsBridge:
    def test_disc_as_legacy_src_args_combines_path(self, tmp_path: Path) -> None:
        d = instantiate(
            "disc",
            args={"host_path": str(tmp_path), "mount_mode": "rw"},
            location={"path": "subdir"},
        )
        src, args = d.as_legacy_src_args()
        assert src == "disc"
        assert args["path"] == os.path.join(str(tmp_path), "subdir")

    def test_s3_as_legacy_src_args_merges_secrets(self) -> None:
        d = instantiate(
            "s3",
            args={"region": "us-east-1", "bucket_default": "buk"},
            secrets={"aws_access_key_id": "AKIA", "aws_secret_access_key": "SEC"},
            location={"path": "prefix/"},
        )
        src, args = d.as_legacy_src_args()
        assert src == "s3"
        assert args["region"] == "us-east-1"
        assert args["bucket"] == "buk"
        assert args["aws_access_key_id"] == "AKIA"
        assert args["prefix"] == "prefix/"


class TestBuildLegacyDriverClass:
    def test_is_idempotent(self) -> None:
        c1 = build_legacy_driver_class("disc")
        c2 = build_legacy_driver_class("disc")
        assert c1 is c2

    def test_creates_distinct_classes_per_type(self) -> None:
        c1 = build_legacy_driver_class("disc")
        c2 = build_legacy_driver_class("s3")
        assert c1 is not c2
        assert c1.type_name == "disc"
        assert c2.type_name == "s3"
