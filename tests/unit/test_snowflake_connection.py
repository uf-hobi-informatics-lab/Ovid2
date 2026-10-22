"""Unit tests for :class:`ovid2.drivers.snowflake.SnowflakeConnection` (legacy shim).

Preserved from the pre-D3.snowflake era; the connection class now
lives at :mod:`ovid2.drivers.snowflake.legacy` (re-exported from
:mod:`ovid2.drivers.snowflake`) and delegates its probe to
:func:`ovid2.drivers.snowflake.probe.probe_auth_from_config`. Every
test value survives byte-identical after the import-path retarget.

The Snowflake connection captures **file locations only** — a
credentials directory plus two filenames (private key + passphrase).
These tests exercise the file-read + PEM/DER conversion pipeline and
the structured error paths surfaced by :meth:`SnowflakeConnection.test`.

None of these tests require ``snowflake-connector-python``; they call
``_build_connect_kwargs`` and ``as_address_config`` directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ovid2.drivers.snowflake import (
    DEFAULT_CREDENTIALS_DIR,
    DEFAULT_PASSPHRASE_FILE,
    DEFAULT_PRIVATE_KEY_FILE,
    SnowflakeConnection,
    _pem_to_der,
    _read_passphrase,
    _read_private_key_pem,
    _resolve_credentials_dir,
)
from ovid2.drivers.snowflake import metadata as sf_conn

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


@pytest.fixture(scope="module")
def rsa_pem_encrypted() -> tuple[bytes, str]:
    """Return a PEM-encoded RSA-2048 private key encrypted with a passphrase."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    passphrase = "correct-horse-battery-staple"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(passphrase.encode()),
    )
    return pem, passphrase


@pytest.fixture
def credentials_dir(
    tmp_path: Path,
    rsa_pem_encrypted: tuple[bytes, str],
) -> Path:
    """Materialise the legacy Ovid layout under a tmp dir.

    ``<tmp>/rsa/rsa_key.p8`` — encrypted PEM key.
    ``<tmp>/rsa/credentials`` — first-line passphrase.
    """
    pem, passphrase = rsa_pem_encrypted
    directory = tmp_path / "rsa"
    directory.mkdir()
    (directory / "rsa_key.p8").write_bytes(pem)
    (directory / "credentials").write_text(passphrase + "\n", encoding="utf-8")
    return directory


# ---------------------------------------------------------------------------
# Default constants — pin the values the field spec advertises.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_defaults_match_legacy_ovid_convention() -> None:
    """Defaults must line up with legacy Ovid's ``common/ovid_secrets.py``."""
    assert DEFAULT_CREDENTIALS_DIR == "/usr/include/rsa"
    assert DEFAULT_PRIVATE_KEY_FILE == "rsa_key.p8"
    assert DEFAULT_PASSPHRASE_FILE == "credentials"


# ---------------------------------------------------------------------------
# _resolve_credentials_dir
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_credentials_dir_uses_default_when_unset() -> None:
    assert _resolve_credentials_dir(None) == Path(DEFAULT_CREDENTIALS_DIR)
    assert _resolve_credentials_dir("") == Path(DEFAULT_CREDENTIALS_DIR)


@pytest.mark.unit
def test_resolve_credentials_dir_expands_user() -> None:
    resolved = _resolve_credentials_dir("~/some/rsa")
    assert not str(resolved).startswith("~")


# ---------------------------------------------------------------------------
# _read_private_key_pem
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_read_private_key_pem_returns_bytes(
    credentials_dir: Path,
    rsa_pem_encrypted: tuple[bytes, str],
) -> None:
    pem, _ = rsa_pem_encrypted
    assert _read_private_key_pem(credentials_dir, "rsa_key.p8") == pem


@pytest.mark.unit
def test_read_private_key_pem_none_when_filename_empty(
    credentials_dir: Path,
) -> None:
    """An empty filename means 'no key configured' — returns ``None``."""
    assert _read_private_key_pem(credentials_dir, "") is None
    assert _read_private_key_pem(credentials_dir, "   ") is None


@pytest.mark.unit
def test_read_private_key_pem_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _read_private_key_pem(tmp_path, "does_not_exist.p8")


# ---------------------------------------------------------------------------
# _read_passphrase
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_read_passphrase_first_line_stripped(credentials_dir: Path) -> None:
    """Trailing newlines / whitespace are stripped from the first line."""
    (credentials_dir / "credentials").write_text(
        "  UWaG51I94dzq  \nsecond line ignored\n", encoding="utf-8"
    )
    assert _read_passphrase(credentials_dir, "credentials") == "UWaG51I94dzq"


@pytest.mark.unit
def test_read_passphrase_none_when_filename_empty(credentials_dir: Path) -> None:
    """No passphrase file → unencrypted key path."""
    assert _read_passphrase(credentials_dir, "") is None
    assert _read_passphrase(credentials_dir, None) is None


@pytest.mark.unit
def test_read_passphrase_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _read_passphrase(tmp_path, "does_not_exist")


@pytest.mark.unit
def test_read_passphrase_empty_file_returns_none(tmp_path: Path) -> None:
    """An empty passphrase file yields ``None`` (unencrypted key)."""
    (tmp_path / "credentials").write_text("", encoding="utf-8")
    assert _read_passphrase(tmp_path, "credentials") is None


# ---------------------------------------------------------------------------
# _pem_to_der
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pem_to_der_unencrypted_roundtrip(rsa_pem: bytes) -> None:
    der = _pem_to_der(rsa_pem, passphrase=None)
    assert isinstance(der, bytes) and der[:1] == b"\x30"
    assert len(der) > 100


@pytest.mark.unit
def test_pem_to_der_encrypted_wrong_passphrase_raises(
    rsa_pem_encrypted: tuple[bytes, str],
) -> None:
    pem, _ = rsa_pem_encrypted
    with pytest.raises((ValueError, TypeError)):
        _pem_to_der(pem, passphrase="wrong")


# ---------------------------------------------------------------------------
# SnowflakeConnection._build_connect_kwargs
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_kwargs_key_pair_end_to_end(credentials_dir: Path) -> None:
    """The credentials_dir + two-filename shape produces DER bytes."""
    conn = SnowflakeConnection(
        args={
            "account": "acme-east",
            "user": "svc",
            "warehouse": "wh",
            "role": "r",
            "authenticator": "SNOWFLAKE",
            "credentials_dir": str(credentials_dir),
            "private_key_file": "rsa_key.p8",
            "private_key_passphrase_file": "credentials",
        },
        secrets={},
    )
    kwargs = conn._build_connect_kwargs()
    assert kwargs["account"] == "acme-east"
    assert kwargs["user"] == "svc"
    assert kwargs["warehouse"] == "wh"
    assert kwargs["role"] == "r"
    assert kwargs["authenticator"] == "SNOWFLAKE"
    assert isinstance(kwargs["private_key"], bytes)
    assert kwargs["private_key"][:1] == b"\x30"
    # File locations are not leaked into the driver kwargs.
    assert "credentials_dir" not in kwargs
    assert "private_key_file" not in kwargs


@pytest.mark.unit
def test_build_kwargs_password_takes_precedence(credentials_dir: Path) -> None:
    """A ``password`` secret bypasses the key-pair path entirely."""
    conn = SnowflakeConnection(
        args={
            "account": "a",
            "user": "u",
            "credentials_dir": str(credentials_dir),
        },
        secrets={"password": "hunter2"},
    )
    kwargs = conn._build_connect_kwargs()
    assert kwargs["password"] == "hunter2"
    assert "private_key" not in kwargs


@pytest.mark.unit
def test_build_kwargs_uses_legacy_defaults_when_only_dir_given(
    credentials_dir: Path,
) -> None:
    """Filenames default to ``rsa_key.p8`` / ``credentials`` per legacy Ovid."""
    conn = SnowflakeConnection(
        args={
            "account": "a",
            "user": "u",
            "credentials_dir": str(credentials_dir),
        },
        secrets={},
    )
    kwargs = conn._build_connect_kwargs()
    assert isinstance(kwargs["private_key"], bytes)


@pytest.mark.unit
def test_build_kwargs_unencrypted_key(
    tmp_path: Path,
    rsa_pem: bytes,
) -> None:
    """A blank passphrase filename means the key is unencrypted."""
    (tmp_path / "rsa_key.p8").write_bytes(rsa_pem)
    conn = SnowflakeConnection(
        args={
            "account": "a",
            "user": "u",
            "credentials_dir": str(tmp_path),
            "private_key_passphrase_file": "",
        },
        secrets={},
    )
    kwargs = conn._build_connect_kwargs()
    assert isinstance(kwargs["private_key"], bytes)


# ---------------------------------------------------------------------------
# SnowflakeConnection.test — structured error codes
# ---------------------------------------------------------------------------


def _stub_snowflake_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a no-op ``snowflake.connector`` in ``sys.modules``.

    Any test that expects the code to short-circuit before contacting
    a real Snowflake account uses this to make ``sf.connect`` fail
    loudly if it is ever called.
    """
    import sys
    import types

    fake = types.ModuleType("snowflake.connector")
    fake.connect = lambda **_: pytest.fail(  # type: ignore[attr-defined]
        "sf.connect must not be called on this test path"
    )
    monkeypatch.setitem(sys.modules, "snowflake", types.ModuleType("snowflake"))
    monkeypatch.setitem(sys.modules, "snowflake.connector", fake)


@pytest.mark.unit
def test_test_returns_auth_on_missing_key_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_snowflake_driver(monkeypatch)
    conn = SnowflakeConnection(
        args={
            "account": "a",
            "user": "u",
            "credentials_dir": str(tmp_path),
            "private_key_file": "nope.p8",
        },
        secrets={},
    )
    result = conn.test()
    assert result.ok is False
    assert result.code == "auth"
    assert "not found" in result.message.lower()


@pytest.mark.unit
def test_test_returns_auth_on_missing_passphrase_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rsa_pem_encrypted: tuple[bytes, str],
) -> None:
    """Key present, passphrase file missing → ``code='auth'``, no crash."""
    _stub_snowflake_driver(monkeypatch)
    pem, _ = rsa_pem_encrypted
    (tmp_path / "rsa_key.p8").write_bytes(pem)
    # deliberately no ``credentials`` file
    conn = SnowflakeConnection(
        args={
            "account": "a",
            "user": "u",
            "credentials_dir": str(tmp_path),
            "private_key_passphrase_file": "credentials",
        },
        secrets={},
    )
    result = conn.test()
    assert result.ok is False
    assert result.code == "auth"


@pytest.mark.unit
def test_test_returns_auth_on_bad_passphrase(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rsa_pem_encrypted: tuple[bytes, str],
) -> None:
    """Wrong passphrase in the credentials file → ``code='auth'``."""
    _stub_snowflake_driver(monkeypatch)
    pem, _ = rsa_pem_encrypted
    (tmp_path / "rsa_key.p8").write_bytes(pem)
    (tmp_path / "credentials").write_text("wrong-pw\n", encoding="utf-8")
    conn = SnowflakeConnection(
        args={
            "account": "a",
            "user": "u",
            "credentials_dir": str(tmp_path),
        },
        secrets={},
    )
    result = conn.test()
    assert result.ok is False
    assert result.code == "auth"


@pytest.mark.unit
def test_test_returns_skipped_when_driver_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    real_import = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "snowflake.connector" or name.startswith("snowflake.connector."):
            raise ImportError("stubbed for test")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    conn = SnowflakeConnection(args={"account": "a", "user": "u"}, secrets={})
    result = conn.test()
    assert result.ok is False
    assert result.code == "skipped"


# ---------------------------------------------------------------------------
# as_address_config
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_as_address_config_produces_der_bytes(credentials_dir: Path) -> None:
    """Pipeline-side dict carries DER bytes, not paths."""
    conn = SnowflakeConnection(
        args={
            "account": "a",
            "user": "u",
            "warehouse": "wh",
            "credentials_dir": str(credentials_dir),
        },
        secrets={},
    )
    merged = conn.as_address_config({"database": "DB", "schema": "S"})
    assert merged["account"] == "a"
    assert merged["user"] == "u"
    assert merged["warehouse"] == "wh"
    assert merged["database"] == "DB"
    assert merged["schema"] == "S"
    assert isinstance(merged["private_key"], bytes)
    assert merged["private_key"][:1] == b"\x30"
    # File locations must not leak into the pipeline-side dict.
    assert "credentials_dir" not in merged
    assert "private_key_file" not in merged


@pytest.mark.unit
def test_as_address_config_password_skips_key_load(tmp_path: Path) -> None:
    """When password auth is configured, no key file is ever read."""
    # Note: no key file exists under tmp_path — if the code tried to
    # read it we would see FileNotFoundError being swallowed. We
    # assert the resulting dict shape instead.
    conn = SnowflakeConnection(
        args={
            "account": "a",
            "user": "u",
            "credentials_dir": str(tmp_path),
        },
        secrets={"password": "hunter2"},
    )
    merged = conn.as_address_config({"database": "DB"})
    assert merged["password"] == "hunter2"
    assert "private_key" not in merged
    assert merged["database"] == "DB"


@pytest.mark.unit
def test_as_address_config_swallows_key_errors(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing key file is logged, not raised — the address loader keeps going."""
    conn = SnowflakeConnection(
        args={
            "account": "a",
            "user": "u",
            "credentials_dir": str(tmp_path),
            "private_key_file": "does_not_exist.p8",
        },
        secrets={},
    )
    with caplog.at_level("WARNING", logger=sf_conn.__name__):
        merged = conn.as_address_config({"database": "DB"})
    assert "private_key" not in merged
    assert merged["database"] == "DB"
    assert any("private key" in rec.message.lower() for rec in caplog.records)
