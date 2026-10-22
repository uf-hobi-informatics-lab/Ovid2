"""Unit tests for :mod:`ovid2.drivers.snowflake.probe`.

Locks the invariants of :func:`probe_auth_from_config` +
:func:`build_connect_kwargs`:

- When ``snowflake-connector-python`` is not installed the probe
  returns ``code='skipped'`` — never raises ``ImportError``.
- Missing credentials files surface as ``code='auth'``.
- PEM parse failures surface as ``code='auth'``.
- Connector-level failures surface as ``code='unreachable'``.
- Query-level failures (after a successful connect) surface as
  ``code='auth'``.
- The four credential-file helpers behave exactly the same way the
  pre-D3.snowflake private helpers did.

The Snowflake driver is optional: this suite installs a minimal fake
``snowflake.connector`` in :data:`sys.modules` via
``monkeypatch.setitem`` so the tests run regardless of whether
``snowflake-connector-python`` is available on the host.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ovid2.drivers.snowflake.probe import (
    _pem_to_der,
    _read_passphrase,
    _read_private_key_pem,
    _resolve_credentials_dir,
    build_connect_kwargs,
    probe_auth_from_config,
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


def _stub_snowflake_driver(
    monkeypatch: pytest.MonkeyPatch,
    *,
    connect: MagicMock | None = None,
) -> MagicMock:
    """Register a minimal fake ``snowflake.connector`` in ``sys.modules``.

    Returns the ``connect`` mock so tests can introspect it.
    """
    if connect is None:
        connect = MagicMock(name="snowflake.connector.connect")
    fake = types.ModuleType("snowflake.connector")
    fake.connect = connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "snowflake", types.ModuleType("snowflake"))
    monkeypatch.setitem(sys.modules, "snowflake.connector", fake)
    return connect


# ---------------------------------------------------------------------------
# Credential-file helpers
# ---------------------------------------------------------------------------


class TestResolveCredentialsDir:
    def test_default_when_none(self) -> None:
        result = _resolve_credentials_dir(None)
        assert result == Path("/usr/include/rsa")

    def test_default_when_empty(self) -> None:
        result = _resolve_credentials_dir("")
        assert result == Path("/usr/include/rsa")

    def test_expanduser_applied(self, tmp_path: Path) -> None:
        # A plain path passes through unchanged.
        result = _resolve_credentials_dir(str(tmp_path))
        assert result == tmp_path


class TestReadPrivateKeyPem:
    def test_returns_bytes(self, tmp_path: Path, rsa_pem: bytes) -> None:
        (tmp_path / "rsa_key.p8").write_bytes(rsa_pem)
        assert _read_private_key_pem(tmp_path, "rsa_key.p8") == rsa_pem

    def test_none_when_filename_blank(self, tmp_path: Path) -> None:
        assert _read_private_key_pem(tmp_path, "") is None
        assert _read_private_key_pem(tmp_path, "   ") is None
        assert _read_private_key_pem(tmp_path, None) is None

    def test_raises_when_missing(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            _read_private_key_pem(tmp_path, "missing.p8")


class TestReadPassphrase:
    def test_first_line_returned(self, tmp_path: Path) -> None:
        (tmp_path / "creds").write_text("secret_pw\ntrailing junk\n")
        assert _read_passphrase(tmp_path, "creds") == "secret_pw"

    def test_none_when_filename_blank(self, tmp_path: Path) -> None:
        assert _read_passphrase(tmp_path, "") is None
        assert _read_passphrase(tmp_path, None) is None

    def test_stripped_whitespace(self, tmp_path: Path) -> None:
        (tmp_path / "creds").write_text("  padded  \n")
        assert _read_passphrase(tmp_path, "creds") == "padded"

    def test_empty_file_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "creds").write_text("")
        assert _read_passphrase(tmp_path, "creds") is None


class TestPemToDer:
    def test_unencrypted_roundtrip(self, rsa_pem: bytes) -> None:
        der = _pem_to_der(rsa_pem, None)
        assert isinstance(der, bytes)
        # DER PKCS8 should be shorter than the base64-encoded PEM.
        assert len(der) < len(rsa_pem)

    def test_encrypted_needs_correct_passphrase(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        encrypted = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.BestAvailableEncryption(b"pw"),
        )
        der = _pem_to_der(encrypted, "pw")
        assert isinstance(der, bytes)

    def test_encrypted_wrong_passphrase_raises(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        encrypted = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.BestAvailableEncryption(b"pw"),
        )
        with pytest.raises(ValueError):
            _pem_to_der(encrypted, "wrong")


# ---------------------------------------------------------------------------
# ``build_connect_kwargs``
# ---------------------------------------------------------------------------


class TestBuildConnectKwargsPasswordAuth:
    def test_password_short_circuits_pem_load(self, tmp_path: Path) -> None:
        kwargs = build_connect_kwargs(
            args={
                "account": "acct",
                "user": "u",
                "credentials_dir": str(tmp_path / "does_not_exist"),
            },
            secrets={"password": "p"},
        )
        assert kwargs["password"] == "p"
        assert "private_key" not in kwargs

    def test_optional_args_populated(self, tmp_path: Path) -> None:
        kwargs = build_connect_kwargs(
            args={
                "account": "acct",
                "user": "u",
                "warehouse": "WH",
                "role": "R",
                "authenticator": "externalbrowser",
            },
            secrets={"password": "p"},
        )
        assert kwargs["warehouse"] == "WH"
        assert kwargs["role"] == "R"
        assert kwargs["authenticator"] == "externalbrowser"


class TestBuildConnectKwargsKeyPairAuth:
    def test_unencrypted_key(self, tmp_path: Path, rsa_pem: bytes) -> None:
        (tmp_path / "rsa_key.p8").write_bytes(rsa_pem)
        kwargs = build_connect_kwargs(
            args={
                "account": "acct",
                "user": "u",
                "credentials_dir": str(tmp_path),
                "private_key_file": "rsa_key.p8",
                "private_key_passphrase_file": "",
            },
            secrets={},
        )
        assert isinstance(kwargs["private_key"], bytes)


# ---------------------------------------------------------------------------
# ``probe_auth_from_config``
# ---------------------------------------------------------------------------


class TestProbeSkipsWhenDriverAbsent:
    def test_missing_snowflake_connector(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setitem(sys.modules, "snowflake", None)
        monkeypatch.setitem(sys.modules, "snowflake.connector", None)
        result = probe_auth_from_config({"account": "a", "user": "u"}, {})
        assert result.code == "skipped"
        assert "snowflake-connector-python" in (result.message or "")


class TestProbeAuthFailures:
    def test_missing_key_file_returns_auth(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _stub_snowflake_driver(monkeypatch)
        result = probe_auth_from_config(
            {
                "account": "a",
                "user": "u",
                "credentials_dir": str(tmp_path),
                "private_key_file": "does_not_exist.p8",
            },
            {},
        )
        assert result.code == "auth"
        assert "not found" in (result.message or "").lower()

    def test_bad_passphrase_returns_auth(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        _stub_snowflake_driver(monkeypatch)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        encrypted = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.BestAvailableEncryption(b"right"),
        )
        (tmp_path / "rsa_key.p8").write_bytes(encrypted)
        (tmp_path / "credentials").write_text("wrong\n")
        result = probe_auth_from_config(
            {
                "account": "a",
                "user": "u",
                "credentials_dir": str(tmp_path),
            },
            {},
        )
        assert result.code == "auth"

    def test_connect_failure_returns_unreachable(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        connect = MagicMock(side_effect=RuntimeError("host down"))
        _stub_snowflake_driver(monkeypatch, connect=connect)
        result = probe_auth_from_config(
            {"account": "a", "user": "u"},
            {"password": "p"},
        )
        assert result.code == "unreachable"

    def test_query_failure_returns_auth(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_conn = MagicMock(name="SnowflakeConnection")
        fake_cursor = MagicMock(name="Cursor")
        fake_cursor.execute.side_effect = RuntimeError("permission denied")
        fake_conn.cursor.return_value = fake_cursor
        connect = MagicMock(return_value=fake_conn)
        _stub_snowflake_driver(monkeypatch, connect=connect)
        result = probe_auth_from_config(
            {"account": "a", "user": "u"},
            {"password": "p"},
        )
        assert result.code == "auth"
        fake_conn.close.assert_called_once()


class TestProbeSuccess:
    def test_happy_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_conn = MagicMock(name="SnowflakeConnection")
        fake_cursor = MagicMock(name="Cursor")
        fake_cursor.fetchone.return_value = (1,)
        fake_conn.cursor.return_value = fake_cursor
        connect = MagicMock(return_value=fake_conn)
        _stub_snowflake_driver(monkeypatch, connect=connect)
        result = probe_auth_from_config(
            {"account": "myaccount", "user": "u"},
            {"password": "p"},
        )
        assert result.ok is True
        assert result.code == "ok"
        assert result.capability == "auth"
        assert result.detail["account"] == "myaccount"
        fake_cursor.execute.assert_called_once_with("SELECT 1")
        fake_conn.close.assert_called_once()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run with pytest.")
