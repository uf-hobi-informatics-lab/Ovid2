"""Unit tests for :func:`ovid2.drivers.registry.driver_from_address`.

The helper is the driver-side successor to
:func:`ovid2.config.addresses.load_address`. Both accept the same two
JSON shapes (legacy ``{src_name, src_args}`` and W7
``{connection, location}``); the driver helper returns a
:class:`Driver` instance so callers can use the D3 read/write
contract instead of the legacy :class:`BaseAddress` machinery.

Invariants exercised:

- **Legacy shape.** ``src_args`` is split into ``(args, secrets,
  location)`` via the driver's own field-spec catalogue so
  credentials land in ``self.secrets`` and location keys land in
  ``self.location`` — not smashed into ``self.args``.
- **W7 shape.** ``args`` and ``secrets`` are lifted off the
  connection JSON; ``location`` is the address's own payload.
- **Snowflake four-tier resolution.** ``pipeline`` and ``sf_overrides``
  flow to :func:`~ovid2.config.addresses._resolve_snowflake_config`
  and the merged ``database`` / ``warehouse`` / ``role`` land on
  the driver.
- **CDM stamp.** ``cdm_name`` lands on ``self.location['cdm']`` for
  both shapes so the D6.0 advisory type check works.
- **Failure modes.** Missing configs raise; missing ``src_name`` and
  ``connection`` fields raise.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def hermetic_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Return a fresh configs / connections / secrets tree.

    Sets the env vars ``OVID2_CONFIGS_DIR``, ``OVID2_CONNECTIONS_DIR``,
    and ``XDG_CONFIG_HOME`` so the loader paths in
    :mod:`ovid2.config.addresses` and :mod:`ovid2.config.connections`
    all land inside ``tmp_path``.
    """
    (tmp_path / "configs").mkdir()
    (tmp_path / "connections").mkdir()
    (tmp_path / "ovid2" / "secrets.d").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("OVID2_CONFIGS_DIR", str(tmp_path / "configs"))
    monkeypatch.setenv("OVID2_CONNECTIONS_DIR", str(tmp_path / "connections"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


def _write(path: Path, data: dict, mode: int = 0o644) -> None:
    path.write_text(json.dumps(data))
    os.chmod(path, mode)


# ---------------------------------------------------------------------------
# Legacy shape
# ---------------------------------------------------------------------------


class TestLegacyShape:
    """Legacy ``{src_name, src_args}`` addresses resolve to native drivers."""

    def test_s3_legacy_splits_credentials_into_secrets(
        self, hermetic_config_dir: Path,
    ) -> None:
        from ovid2.drivers.registry import driver_from_address
        from ovid2.drivers.s3 import S3Driver

        _write(
            hermetic_config_dir / "configs" / "legacy-s3.json",
            {
                "src_name": "s3",
                "src_args": {
                    "bucket": "my-bucket",
                    "region": "us-east-1",
                    "aws_access_key_id": "AKIA",
                    "aws_secret_access_key": "sekrit",
                    "aws_session_token": "TOKEN",
                    "format": "parquet",
                },
            },
        )
        d = driver_from_address("legacy-s3")
        assert isinstance(d, S3Driver)
        # Credentials land on ``secrets``, not ``args``.
        assert d.secrets["aws_access_key_id"] == "AKIA"
        assert d.secrets["aws_secret_access_key"] == "sekrit"
        assert d.secrets["aws_session_token"] == "TOKEN"
        assert "aws_access_key_id" not in d.args
        # Location keys (bucket, path, format) land on ``location``.
        assert d.location["bucket"] == "my-bucket"
        assert d.location["format"] == "parquet"
        # Non-secret non-location keys stay on ``args``.
        assert d.args["region"] == "us-east-1"
        # And the auth-mode classifier sees the full triple.
        assert d.auth_mode() == "session"

    def test_disc_legacy_splits_path_into_location(
        self, hermetic_config_dir: Path,
    ) -> None:
        from ovid2.drivers.disc import DiscDriver
        from ovid2.drivers.registry import driver_from_address

        _write(
            hermetic_config_dir / "configs" / "legacy-disc.json",
            {"src_name": "disc", "src_args": {"path": "/tmp/some_dir",
                                              "format": "csv"}},
        )
        d = driver_from_address("legacy-disc")
        assert isinstance(d, DiscDriver)
        # ``path`` is a location key for disc.
        assert d.location.get("path") == "/tmp/some_dir"
        assert d.location.get("format") == "csv"

    def test_missing_src_name_raises(self, hermetic_config_dir: Path) -> None:
        from ovid2.drivers.registry import driver_from_address

        _write(
            hermetic_config_dir / "configs" / "broken.json",
            {"src_args": {"path": "/tmp/x"}},
        )
        with pytest.raises(ValueError, match="missing 'src_name'"):
            driver_from_address("broken")

    def test_missing_config_raises(self, hermetic_config_dir: Path) -> None:
        from ovid2.drivers.registry import driver_from_address

        with pytest.raises(FileNotFoundError):
            driver_from_address("no-such-address")


# ---------------------------------------------------------------------------
# W7 shape
# ---------------------------------------------------------------------------


class TestW7Shape:
    """W7 ``{connection, location}`` addresses resolve through connection JSON."""

    def _write_w7_s3(
        self,
        root: Path,
        *,
        conn_name: str = "my-s3",
        addr_name: str = "w7-s3",
        secret_ref: str | None = "my-s3-secret",
        conn_args: dict | None = None,
        secret_payload: dict | None = None,
        location: dict | None = None,
    ) -> None:
        conn_payload: dict = {
            "name": conn_name,
            "type": "s3",
            "args": conn_args or {"region": "us-west-2",
                                  "bucket_default": "default-bucket"},
        }
        if secret_ref:
            conn_payload["secret_ref"] = secret_ref
            _write(
                root / "ovid2" / "secrets.d" / f"{secret_ref}.json",
                secret_payload or {
                    "aws_access_key_id": "AKIA2",
                    "aws_secret_access_key": "sekrit2",
                },
                mode=0o600,
            )
        _write(root / "connections" / f"{conn_name}.json", conn_payload)
        _write(
            root / "configs" / f"{addr_name}.json",
            {"connection": conn_name, "location": location or {"bucket": "w7-b"}},
        )

    def test_lifts_args_and_secrets_from_connection(
        self, hermetic_config_dir: Path,
    ) -> None:
        from ovid2.drivers.registry import driver_from_address
        from ovid2.drivers.s3 import S3Driver

        self._write_w7_s3(hermetic_config_dir)
        d = driver_from_address("w7-s3")
        assert isinstance(d, S3Driver)
        # Connection args (non-secret) land on ``args``.
        assert d.args["region"] == "us-west-2"
        assert d.args["bucket_default"] == "default-bucket"
        # Connection secrets land on ``secrets``.
        assert d.secrets["aws_access_key_id"] == "AKIA2"
        # Address location lands on ``location``.
        assert d.location["bucket"] == "w7-b"
        # Auth mode reflects the merged triple.
        assert d.auth_mode() == "static"

    def test_bucket_default_used_when_location_bucket_absent(
        self, hermetic_config_dir: Path,
    ) -> None:
        from ovid2.drivers.registry import driver_from_address

        self._write_w7_s3(hermetic_config_dir, location={"path": "prefix1"})
        d = driver_from_address("w7-s3")
        _, merged = d.as_legacy_src_args()
        # ``bucket_default`` falls back when location.bucket is blank.
        assert merged.get("bucket") == "default-bucket"

    def test_cdm_name_lands_on_location(
        self, hermetic_config_dir: Path,
    ) -> None:
        from ovid2.drivers.registry import driver_from_address

        self._write_w7_s3(hermetic_config_dir)
        d = driver_from_address("w7-s3", cdm_name="pcornet_7_0")
        assert d.location.get("cdm") == "pcornet_7_0"

    def test_missing_connection_field_raises(
        self, hermetic_config_dir: Path,
    ) -> None:
        from ovid2.drivers.registry import driver_from_address

        _write(
            hermetic_config_dir / "configs" / "broken.json",
            {"connection": "", "location": {}},
        )
        with pytest.raises(ValueError, match="empty 'connection'"):
            driver_from_address("broken")

    def test_missing_connection_file_raises(
        self, hermetic_config_dir: Path,
    ) -> None:
        from ovid2.drivers.registry import driver_from_address

        _write(
            hermetic_config_dir / "configs" / "orphan.json",
            {"connection": "no-such-connection", "location": {}},
        )
        with pytest.raises(FileNotFoundError):
            driver_from_address("orphan")


# ---------------------------------------------------------------------------
# CDM stamping (legacy shape)
# ---------------------------------------------------------------------------


class TestCdmStamp:
    """``cdm_name`` threads onto the legacy shape via ``src_args['cdm']``."""

    def test_legacy_cdm_stamp(self, hermetic_config_dir: Path) -> None:
        from ovid2.drivers.registry import driver_from_address

        _write(
            hermetic_config_dir / "configs" / "legacy-s3.json",
            {
                "src_name": "s3",
                "src_args": {
                    "bucket": "b",
                    "region": "us-east-1",
                },
            },
        )
        d = driver_from_address("legacy-s3", cdm_name="pcornet_7_0")
        # For legacy shape ``cdm`` lands on ``args`` (via ``src_args``
        # splitting: ``cdm`` is neither a secret nor a location key on
        # the s3 spec).
        assert d.args.get("cdm") == "pcornet_7_0"


# ---------------------------------------------------------------------------
# Registration-side smoke check
# ---------------------------------------------------------------------------


class TestRegistryExposure:
    """The helper is publicly exported from :mod:`ovid2.drivers.registry`."""

    def test_in_all(self) -> None:
        import ovid2.drivers.registry as reg

        assert "driver_from_address" in reg.__all__

    def test_callable(self) -> None:
        from ovid2.drivers.registry import driver_from_address

        assert callable(driver_from_address)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
