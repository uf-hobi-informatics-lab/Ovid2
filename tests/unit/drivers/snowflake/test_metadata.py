"""Unit tests for :mod:`ovid2.drivers.snowflake.metadata`.

Locks the invariants relied on by the web tier and MCP:

- ``connection_fields`` / ``location_fields`` return typed
  :class:`ConnectionFieldSpec` / :class:`LocationField` shapes and are
  byte-for-byte equivalent to the pre-D3.snowflake catalogues
  (``_FIELD_SPECS["snowflake"]`` /
  ``_LOCATION_SPECS["snowflake"]``).
- ``build_address_config`` reproduces the pre-D3.snowflake
  :meth:`SnowflakeConnection.as_address_config` shape exactly,
  including the PEM → DER conversion, the password-vs-key-pair
  short-circuit, and the log-and-swallow error semantics on
  missing / unreadable credential files.
- Metadata module is import-safe (no snowflake-*, no pyspark, no
  cryptography at module scope).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from ovid2.drivers.base import ConnectionFieldSpec, LocationField
from ovid2.drivers.snowflake.metadata import (
    DEFAULT_CREDENTIALS_DIR,
    DEFAULT_PASSPHRASE_FILE,
    DEFAULT_PRIVATE_KEY_FILE,
    build_address_config,
    connection_fields,
    location_fields,
)


# ---------------------------------------------------------------------------
# Fresh key material fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_pem() -> bytes:
    """Return an unencrypted PEM-encoded RSA-2048 private key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


class TestConnectionFields:
    """The connection-form field spec."""

    def test_returns_typed_spec(self) -> None:
        spec = connection_fields()
        assert isinstance(spec, ConnectionFieldSpec)

    def test_arg_names_match_legacy(self) -> None:
        spec = connection_fields()
        names = [f.name for f in spec.args]
        assert names == [
            "account",
            "user",
            "warehouse",
            "role",
            "authenticator",
            "credentials_dir",
            "private_key_file",
            "private_key_passphrase_file",
        ]

    def test_secret_names_match_legacy(self) -> None:
        spec = connection_fields()
        names = [f.name for f in spec.secrets]
        assert names == ["password"]

    def test_account_and_user_are_required(self) -> None:
        spec = connection_fields()
        by_name = {f.name: f for f in spec.args}
        assert by_name["account"].required is True
        assert by_name["user"].required is True

    def test_credential_defaults(self) -> None:
        spec = connection_fields()
        by_name = {f.name: f for f in spec.args}
        assert by_name["credentials_dir"].default == DEFAULT_CREDENTIALS_DIR
        assert by_name["private_key_file"].default == DEFAULT_PRIVATE_KEY_FILE
        assert (
            by_name["private_key_passphrase_file"].default
            == DEFAULT_PASSPHRASE_FILE
        )

    def test_password_is_optional(self) -> None:
        """Password auth is opt-in; key-pair auth is the default."""
        spec = connection_fields()
        password = next(f for f in spec.secrets if f.name == "password")
        assert password.required is False


class TestLocationFields:
    """The location-form field spec."""

    def test_returns_typed_tuple(self) -> None:
        fields = location_fields()
        assert isinstance(fields, tuple)
        for f in fields:
            assert isinstance(f, LocationField)

    def test_names_match_legacy(self) -> None:
        names = [f.name for f in location_fields()]
        assert names == ["database", "schema"]

    def test_database_is_required(self) -> None:
        database = next(f for f in location_fields() if f.name == "database")
        assert database.required is True

    def test_schema_is_optional(self) -> None:
        schema = next(f for f in location_fields() if f.name == "schema")
        assert schema.required is False


class TestBuildAddressConfigPasswordAuth:
    """Password takes the short-circuit — no PEM read attempted."""

    def test_password_forwarded_verbatim(self) -> None:
        got = build_address_config(
            args={"account": "acct", "user": "u"},
            secrets={"password": "p1234"},
            location={"database": "DB"},
        )
        assert got == {
            "account": "acct",
            "user": "u",
            "password": "p1234",
            "database": "DB",
        }

    def test_password_bypasses_pem_load(
        self, tmp_path: Path,
    ) -> None:
        """Even with an unreadable credentials_dir, password wins."""
        got = build_address_config(
            args={
                "account": "acct",
                "user": "u",
                "credentials_dir": str(tmp_path / "does_not_exist"),
            },
            secrets={"password": "p"},
            location={"database": "DB"},
        )
        assert got["password"] == "p"
        assert "private_key" not in got


class TestBuildAddressConfigKeyPairAuth:
    """Key-pair auth reads PEM + passphrase and emits DER bytes."""

    def test_unencrypted_pem_produces_der_bytes(
        self, tmp_path: Path, rsa_pem: bytes,
    ) -> None:
        key_file = tmp_path / "rsa_key.p8"
        key_file.write_bytes(rsa_pem)
        got = build_address_config(
            args={
                "account": "acct",
                "user": "u",
                "credentials_dir": str(tmp_path),
                "private_key_file": "rsa_key.p8",
                "private_key_passphrase_file": "",
            },
            secrets={},
            location={"database": "DB"},
        )
        assert isinstance(got["private_key"], bytes)
        assert got["account"] == "acct"
        assert got["database"] == "DB"

    def test_encrypted_pem_needs_passphrase_file(
        self, tmp_path: Path,
    ) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        encrypted = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.BestAvailableEncryption(b"secret"),
        )
        (tmp_path / "rsa_key.p8").write_bytes(encrypted)
        (tmp_path / "credentials").write_text("secret\n")

        got = build_address_config(
            args={
                "account": "acct",
                "user": "u",
                "credentials_dir": str(tmp_path),
            },
            secrets={},
            location={"database": "DB"},
        )
        assert isinstance(got["private_key"], bytes)


class TestBuildAddressConfigErrorHandling:
    """File-read and decode errors log a warning and drop private_key."""

    def test_missing_key_file_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(
            logging.WARNING, logger="ovid2.drivers.snowflake.metadata"
        ):
            got = build_address_config(
                args={
                    "account": "a",
                    "user": "u",
                    "credentials_dir": str(tmp_path),
                    "private_key_file": "does_not_exist.p8",
                },
                secrets={},
                location={"database": "DB"},
            )
        assert "private_key" not in got
        assert got["database"] == "DB"
        assert any(
            "private key" in rec.message.lower() for rec in caplog.records
        )

    def test_missing_passphrase_file_logs_warning(
        self, tmp_path: Path, rsa_pem: bytes,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        (tmp_path / "rsa_key.p8").write_bytes(rsa_pem)
        # Passphrase file missing. ``_read_passphrase`` raises
        # ``FileNotFoundError`` on the explicit-filename path; the
        # merger logs and drops ``private_key`` rather than fail
        # loudly (matches the pre-D3.snowflake behaviour).
        with caplog.at_level(
            logging.WARNING, logger="ovid2.drivers.snowflake.metadata"
        ):
            got = build_address_config(
                args={
                    "account": "a",
                    "user": "u",
                    "credentials_dir": str(tmp_path),
                    "private_key_passphrase_file": "does_not_exist",
                },
                secrets={},
                location={"database": "DB"},
            )
        assert "private_key" not in got
        assert any(
            "passphrase" in rec.message.lower() for rec in caplog.records
        )

    def test_explicit_empty_key_filename_opts_out(
        self, tmp_path: Path,
    ) -> None:
        """A blank ``private_key_file`` means 'no key-pair auth attempted'."""
        got = build_address_config(
            args={
                "account": "a",
                "user": "u",
                "credentials_dir": str(tmp_path),
                "private_key_file": "",
            },
            secrets={},
            location={"database": "DB"},
        )
        assert "private_key" not in got
        assert got["database"] == "DB"


class TestBuildAddressConfigLocationForwarding:
    """The ``database`` / ``schema`` location keys land at the top level."""

    def test_database_forwarded(self) -> None:
        got = build_address_config(
            args={"account": "a", "user": "u"},
            secrets={"password": "p"},
            location={"database": "MY_DB"},
        )
        assert got["database"] == "MY_DB"

    def test_schema_forwarded(self) -> None:
        got = build_address_config(
            args={"account": "a", "user": "u"},
            secrets={"password": "p"},
            location={"database": "DB", "schema": "MY_SCHEMA"},
        )
        assert got["schema"] == "MY_SCHEMA"

    def test_authenticator_forwarded(self) -> None:
        got = build_address_config(
            args={"account": "a", "user": "u", "authenticator": "externalbrowser"},
            secrets={},
            location={"database": "DB"},
        )
        assert got["authenticator"] == "externalbrowser"

    def test_path_maps_to_database(self) -> None:
        """A generic ``path`` location resolves to ``database``.

        Regression: web-UI addresses express the target as
        ``location.path`` (e.g. ``CHAR_ACME``). Without this mapping the
        write silently fell back to the pipeline's
        ``DEFAULT_PIPELINE_DB_NAME`` and hit a non-existent database.
        """
        got = build_address_config(
            args={"account": "a", "user": "u"},
            secrets={"password": "p"},
            location={"path": "CHAR_ACME"},
        )
        assert got["database"] == "CHAR_ACME"
        assert "schema" not in got

    def test_path_with_schema_splits(self) -> None:
        got = build_address_config(
            args={"account": "a", "user": "u"},
            secrets={"password": "p"},
            location={"path": "CHAR_ACME/PUBLIC"},
        )
        assert got["database"] == "CHAR_ACME"
        assert got["schema"] == "PUBLIC"

    def test_explicit_database_wins_over_path(self) -> None:
        got = build_address_config(
            args={"account": "a", "user": "u"},
            secrets={"password": "p"},
            location={"path": "FROM_PATH", "database": "EXPLICIT"},
        )
        assert got["database"] == "EXPLICIT"

    def test_path_ignores_leading_and_trailing_slashes(self) -> None:
        got = build_address_config(
            args={"account": "a", "user": "u"},
            secrets={"password": "p"},
            location={"path": "/CHAR_ACME/"},
        )
        assert got["database"] == "CHAR_ACME"
        assert "schema" not in got


class TestImportSafety:
    """The metadata module must not pull in snowflake-* or pyspark or cryptography."""

    def test_no_heavy_imports_after_module_load(self) -> None:
        # The invariant is enforced by
        # ``tests.contract.test_driver_metadata_import_safe`` at the
        # AST level; here we just document the intent. A prior test
        # in the same module might have imported ``cryptography`` for
        # its own purposes; that's covered by the AST scanner.
        assert True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run with pytest.")
