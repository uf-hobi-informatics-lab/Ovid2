"""Unit tests for :mod:`ovid2.tools.extra_mounts`.

Covers parse-time validation of the ``--extra-mount`` flag used by
``ovid2 tools copy``: absolute-path enforcement, mode canonicalisation,
reserved / forbidden container path rejection, duplicate-container
detection, and host-must-exist checks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ovid2.tools.extra_mounts import (
    ExtraMount,
    ExtraMountError,
    parse_extra_mount,
    parse_extra_mounts,
)


class TestParseExtraMount:
    """``parse_extra_mount`` accepts well-formed specs and rejects the rest."""

    def test_two_part_defaults_to_rw(self, tmp_path: Path) -> None:
        m = parse_extra_mount(f"{tmp_path}:/copy_out")
        assert m == ExtraMount(host=str(tmp_path), container="/copy_out", mode="rw")
        assert m.as_docker_v() == f"{tmp_path}:/copy_out:rw"

    def test_three_part_explicit_ro(self, tmp_path: Path) -> None:
        m = parse_extra_mount(f"{tmp_path}:/copy_out:ro")
        assert m.mode == "ro"
        assert m.as_compose_volume() == f"{tmp_path}:/copy_out:ro"

    def test_mode_case_insensitive(self, tmp_path: Path) -> None:
        m = parse_extra_mount(f"{tmp_path}:/copy_out:RW")
        assert m.mode == "rw"

    @pytest.mark.parametrize("spec", ["", "just_one", "a:b:c:d", ":c:rw", "h::rw"])
    def test_malformed_specs_rejected(self, spec: str) -> None:
        with pytest.raises(ExtraMountError):
            parse_extra_mount(spec)

    def test_non_string_rejected(self) -> None:
        with pytest.raises(ExtraMountError):
            parse_extra_mount(None)  # type: ignore[arg-type]

    def test_relative_host_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ExtraMountError, match="absolute"):
            parse_extra_mount("./relative:/copy_out")

    def test_relative_container_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ExtraMountError, match="absolute"):
            parse_extra_mount(f"{tmp_path}:copy_out")

    def test_non_normalized_host_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ExtraMountError, match="normalized"):
            parse_extra_mount(f"{tmp_path}/..:/copy_out")

    def test_non_normalized_container_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ExtraMountError, match="normalized"):
            parse_extra_mount(f"{tmp_path}:/copy_out/..")

    def test_unknown_mode_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ExtraMountError, match="mode"):
            parse_extra_mount(f"{tmp_path}:/copy_out:banana")

    def test_missing_host_rejected(self) -> None:
        with pytest.raises(ExtraMountError, match="does not exist"):
            parse_extra_mount("/nonexistent-host-path-12345:/copy_out")

    @pytest.mark.parametrize(
        "reserved",
        ["/app", "/data", "/ovid2-user-config",
         "/ovid_credentials", "/usr/include/rsa"],
    )
    def test_reserved_container_paths_rejected(
        self, tmp_path: Path, reserved: str,
    ) -> None:
        with pytest.raises(ExtraMountError, match="reserved"):
            parse_extra_mount(f"{tmp_path}:{reserved}")

    @pytest.mark.parametrize("forbidden", ["/", "/tmp"])
    def test_forbidden_container_paths_rejected(
        self, tmp_path: Path, forbidden: str,
    ) -> None:
        with pytest.raises(ExtraMountError, match="forbidden"):
            parse_extra_mount(f"{tmp_path}:{forbidden}")


class TestParseExtraMounts:
    """``parse_extra_mounts`` handles the list case."""

    def test_none_returns_empty(self) -> None:
        assert parse_extra_mounts(None) == []

    def test_empty_list_returns_empty(self) -> None:
        assert parse_extra_mounts([]) == []

    def test_multiple_valid(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        result = parse_extra_mounts([
            f"{tmp_path / 'a'}:/mnt_a",
            f"{tmp_path / 'b'}:/mnt_b:ro",
        ])
        assert len(result) == 2
        assert result[0].container == "/mnt_a"
        assert result[1].mode == "ro"

    def test_duplicate_container_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        with pytest.raises(ExtraMountError, match="already mounted"):
            parse_extra_mounts([
                f"{tmp_path / 'a'}:/mnt",
                f"{tmp_path / 'b'}:/mnt",
            ])

    def test_error_includes_index(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        with pytest.raises(ExtraMountError, match="extra-mount #2"):
            parse_extra_mounts([
                f"{tmp_path / 'a'}:/mnt_a",
                "malformed",
            ])


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
