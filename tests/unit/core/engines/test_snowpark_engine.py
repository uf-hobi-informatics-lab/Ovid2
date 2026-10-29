"""Unit tests for :mod:`ovid2.core.engines.snowpark`.

Focus on the two credential-sourcing paths — inline (legacy) and via a
named W7 ``snowflake`` connection — plus :meth:`SnowparkEngine.test`
behaviour when the driver is absent or a credential file is missing.

The tests never import ``snowflake.snowpark``; they either patch the
module-level import or exercise pre-``get_session`` config code, so
they run on any host regardless of what Snowflake libraries are
installed.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ovid2.core.engines.protocols import EngineFamily
from ovid2.core.engines.snowpark import SnowparkEngine

# ---------------------------------------------------------------------------
# _configs_inline (legacy path)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_inline_password_path_returns_password_kwargs() -> None:
    """No 'connection' arg + inline password secret → password-auth cfg."""
    engine = SnowparkEngine(
        args={"account": "acct", "user": "usr", "warehouse": "wh"},
        secrets={"password": "s3cret"},
    )
    cfg = engine._build_configs()
    assert cfg["account"] == "acct"
    assert cfg["user"] == "usr"
    assert cfg["warehouse"] == "wh"
    assert cfg["password"] == "s3cret"
    # Session parameters are merged in by default (Change A).
    assert cfg["abort_detached_query"] is True
    assert cfg["client_session_keep_alive"] is True


@pytest.mark.unit
def test_inline_private_key_secret_is_not_stringified() -> None:
    """A DER-bytes secret round-trips as bytes, not str(bytes)."""
    der_bytes = b"\x00\x01\x02fake-der\x03"
    engine = SnowparkEngine(
        args={"account": "acct", "user": "usr"},
        secrets={"private_key": der_bytes},
    )
    cfg = engine._build_configs()
    assert cfg["private_key"] is der_bytes  # not str()-ified


@pytest.mark.unit
def test_inline_missing_account_and_user_raises() -> None:
    """Neither 'connection' nor 'account'/'user' → ValueError names them."""
    engine = SnowparkEngine(args={}, secrets={})
    with pytest.raises(ValueError, match="missing required args"):
        engine._build_configs()


@pytest.mark.unit
def test_inline_missing_credentials_raises() -> None:
    """'account' + 'user' with no password or private_key → clear error."""
    engine = SnowparkEngine(args={"account": "acct", "user": "usr"}, secrets={})
    with pytest.raises(ValueError, match=r"password.*private_key"):
        engine._build_configs()


@pytest.mark.unit
def test_blank_connection_string_treated_as_inline() -> None:
    """A whitespace-only 'connection' value must not trigger the loader."""
    engine = SnowparkEngine(
        args={"connection": "   ", "account": "a", "user": "u"},
        secrets={"password": "p"},
    )
    cfg = engine._build_configs()
    assert cfg["account"] == "a"
    assert cfg["user"] == "u"
    assert cfg["password"] == "p"


# ---------------------------------------------------------------------------
# Session parameters (Change A — kill duplicate CTAS executions)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_session_parameters_applied() -> None:
    """Every session builds with ABORT_DETACHED_QUERY + keep-alive by default."""
    engine = SnowparkEngine(
        args={"account": "acct", "user": "usr"},
        secrets={"password": "p"},
    )
    cfg = engine._build_configs()
    assert cfg["abort_detached_query"] is True
    assert cfg["client_session_keep_alive"] is True


@pytest.mark.unit
def test_engine_session_parameters_override_defaults() -> None:
    """A per-engine ``session_parameters`` arg wins over the settings defaults."""
    engine = SnowparkEngine(
        args={
            "account": "acct",
            "user": "usr",
            "session_parameters": {
                "abort_detached_query": False,
                "statement_timeout_in_seconds": 7200,
            },
        },
        secrets={"password": "p"},
    )
    cfg = engine._build_configs()
    assert cfg["abort_detached_query"] is False  # operator override wins
    assert cfg["statement_timeout_in_seconds"] == 7200
    # Untouched default still present.
    assert cfg["client_session_keep_alive"] is True


@pytest.mark.unit
def test_env_session_parameters_override_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``$OVID2_SNOWPARK_SESSION_PARAMETERS`` merges over the defaults."""
    import importlib

    from ovid2.config import settings as settings_mod

    monkeypatch.setenv(
        "OVID2_SNOWPARK_SESSION_PARAMETERS",
        '{"abort_detached_query": false, "lock_timeout": 60}',
    )
    importlib.reload(settings_mod)
    try:
        engine = SnowparkEngine(
            args={"account": "acct", "user": "usr"},
            secrets={"password": "p"},
        )
        cfg = engine._build_configs()
        assert cfg["abort_detached_query"] is False
        assert cfg["lock_timeout"] == 60
        assert cfg["client_session_keep_alive"] is True
    finally:
        monkeypatch.delenv("OVID2_SNOWPARK_SESSION_PARAMETERS", raising=False)
        importlib.reload(settings_mod)


@pytest.mark.unit
def test_malformed_env_session_parameters_falls_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bad JSON in the env override is ignored — defaults still apply."""
    import importlib

    from ovid2.config import settings as settings_mod

    monkeypatch.setenv("OVID2_SNOWPARK_SESSION_PARAMETERS", "{not json")
    importlib.reload(settings_mod)
    try:
        assert settings_mod.SNOWPARK_SESSION_PARAMETERS["abort_detached_query"] is True
    finally:
        monkeypatch.delenv("OVID2_SNOWPARK_SESSION_PARAMETERS", raising=False)
        importlib.reload(settings_mod)


@pytest.mark.unit
def test_session_parameters_do_not_clobber_auth_keys() -> None:
    """A ``session_parameters`` key that collides with auth must not win."""
    engine = SnowparkEngine(
        args={
            "account": "acct",
            "user": "usr",
            # Malicious/accidental collision: try to override the account
            # through session_parameters — setdefault must protect it.
            "session_parameters": {"account": "WRONG"},
        },
        secrets={"password": "p"},
    )
    cfg = engine._build_configs()
    assert cfg["account"] == "acct"  # real account preserved


# ---------------------------------------------------------------------------
# _configs_from_connection (W7 connection path)
# ---------------------------------------------------------------------------


def _write_connection(
    root: Path, name: str, args: dict[str, Any], secret_payload: dict[str, Any] | None
) -> None:
    """Write a ``$OVID2_CONNECTIONS_DIR/<name>.json`` + optional secret file.

    Mirrors the on-disk shape :mod:`ovid2.config.connections` and
    :mod:`ovid2.config.secrets` expect. Kept local so we don't couple
    the test to those modules' private path helpers.
    """
    conns_dir = root / "connections"
    conns_dir.mkdir(parents=True, exist_ok=True)
    body: dict[str, Any] = {"type": "snowflake", "args": args}
    if secret_payload is not None:
        # secrets loader looks in $XDG_CONFIG_HOME/ovid2/secrets.d/.
        secrets_dir = root / "ovid2" / "secrets.d"
        secrets_dir.mkdir(parents=True, exist_ok=True)
        secret_path = secrets_dir / f"{name}.json"
        secret_path.write_text(json.dumps(secret_payload), encoding="utf-8")
        os.chmod(secret_path, 0o600)
        body["secret_ref"] = name
    (conns_dir / f"{name}.json").write_text(json.dumps(body), encoding="utf-8")


@pytest.fixture
def xdg_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the connection loader + secrets loader at a scratch XDG tree."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OVID2_CONNECTIONS_DIR", str(tmp_path / "connections"))
    return tmp_path


@pytest.mark.unit
def test_connection_password_auth_populates_configs(xdg_home: Path) -> None:
    """A password-auth Snowflake connection feeds the Snowpark builder."""
    _write_connection(
        xdg_home,
        "sf_password",
        {"account": "myaccount", "user": "svc_user", "warehouse": "wh_c"},
        {"password": "conn_pw"},
    )
    engine = SnowparkEngine(args={"connection": "sf_password"})
    cfg = engine._build_configs()
    assert cfg["account"] == "myaccount"
    assert cfg["user"] == "svc_user"
    assert cfg["password"] == "conn_pw"
    # Warehouse from the connection propagates through when the engine
    # does not override it.
    assert cfg["warehouse"] == "wh_c"


@pytest.mark.unit
def test_engine_overrides_connection_warehouse(xdg_home: Path) -> None:
    """The engine's own warehouse/database/schema/role override the connection."""
    _write_connection(
        xdg_home,
        "sf_pw",
        {
            "account": "acct",
            "user": "usr",
            "warehouse": "connection_wh",
            "role": "connection_role",
        },
        {"password": "p"},
    )
    engine = SnowparkEngine(
        args={
            "connection": "sf_pw",
            "warehouse": "engine_wh",
            "database": "engine_db",
            "schema": "engine_schema",
            # role deliberately omitted — connection value should remain
        }
    )
    cfg = engine._build_configs()
    assert cfg["warehouse"] == "engine_wh"
    assert cfg["database"] == "engine_db"
    assert cfg["schema"] == "engine_schema"
    assert cfg["role"] == "connection_role"


@pytest.mark.unit
def test_connection_missing_credentials_raises(xdg_home: Path) -> None:
    """A connection that yields no password / private_key → ValueError.

    Passwordless key-pair auth requires the private-key files under
    ``credentials_dir`` to exist. When the operator points at a
    directory we know is empty, ``build_connect_kwargs`` returns just
    ``account`` + ``user`` and the engine must refuse to launch a
    session that cannot authenticate.
    """
    empty = xdg_home / "empty_rsa"
    empty.mkdir()
    _write_connection(
        xdg_home,
        "sf_broken",
        {
            "account": "acct",
            "user": "usr",
            "credentials_dir": str(empty),
            # Point at a filename that doesn't exist in `empty/` so the
            # connection returns no private_key at all (rather than
            # raising FileNotFoundError, which is a different code path).
            "private_key_file": "",
            "private_key_passphrase_file": "",
        },
        None,
    )
    engine = SnowparkEngine(args={"connection": "sf_broken"})
    with pytest.raises(ValueError, match=r"did not yield.*credentials"):
        engine._build_configs()


@pytest.mark.unit
def test_connection_missing_account_raises(xdg_home: Path) -> None:
    """A connection missing account or user → ValueError names the connection."""
    _write_connection(
        xdg_home,
        "sf_no_account",
        # No 'account' — connection body still writes but yields nothing.
        {"user": "u"},
        {"password": "p"},
    )
    engine = SnowparkEngine(args={"connection": "sf_no_account"})
    with pytest.raises(ValueError, match="did not yield 'account'"):
        engine._build_configs()


@pytest.mark.unit
def test_wrong_connection_type_raises(xdg_home: Path) -> None:
    """Pointing at a non-``snowflake`` connection → clear ValueError."""
    conns_dir = xdg_home / "connections"
    conns_dir.mkdir(parents=True, exist_ok=True)
    (conns_dir / "fs_disc.json").write_text(
        json.dumps({"type": "disc", "args": {"root_path": "/tmp"}}),
        encoding="utf-8",
    )
    engine = SnowparkEngine(args={"connection": "fs_disc"})
    with pytest.raises(ValueError, match="expected 'snowflake'"):
        engine._build_configs()


@pytest.mark.unit
def test_unknown_connection_name_bubbles_file_not_found(xdg_home: Path) -> None:
    """Referencing a connection that doesn't exist → FileNotFoundError.

    The engine deliberately lets this propagate so :meth:`test` can
    convert it to an ``auth`` result with the exact path list.
    """
    engine = SnowparkEngine(args={"connection": "does_not_exist"})
    with pytest.raises(FileNotFoundError):
        engine._build_configs()


# ---------------------------------------------------------------------------
# SnowparkEngine.test — driver missing + credential-file missing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_test_reports_unreachable_when_driver_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``snowflake.snowpark`` on the host → ``unreachable`` result."""
    # Ensure any cached module is gone, then poison the import.
    monkeypatch.delitem(sys.modules, "snowflake.snowpark", raising=False)
    monkeypatch.delitem(sys.modules, "snowflake", raising=False)
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("snowflake"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    engine = SnowparkEngine(args={"account": "a", "user": "u"}, secrets={"password": "p"})
    result = engine.test()
    assert result.ok is False
    assert result.code == "unreachable"
    assert "snowflake-snowpark-python" in result.message


@pytest.mark.unit
def test_test_reports_auth_when_connection_credentials_missing(
    xdg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connection pointing at a missing key file → ``code='auth'``.

    Simulates the common operator mistake of naming a connection whose
    ``private_key_file`` doesn't exist yet. ``build_connect_kwargs``
    raises :class:`FileNotFoundError`; :meth:`SnowparkEngine.test` must
    translate that to ``auth`` rather than the driver-side
    ``unreachable`` bucket.
    """
    # Pretend the driver is importable so the test reaches the config
    # step. We stub `snowflake.snowpark` to a bare module with a
    # never-called `Session`.
    snowpark_mod = types.ModuleType("snowflake.snowpark")
    snowpark_mod.Session = MagicMock(  # type: ignore[attr-defined]
        side_effect=AssertionError("Session must not be built when credentials are missing")
    )
    snowflake_pkg = types.ModuleType("snowflake")
    snowflake_pkg.snowpark = snowpark_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "snowflake", snowflake_pkg)
    monkeypatch.setitem(sys.modules, "snowflake.snowpark", snowpark_mod)

    missing_dir = xdg_home / "does_not_exist_rsa"
    # Note: directory is intentionally NOT created.
    _write_connection(
        xdg_home,
        "sf_bad_path",
        {
            "account": "acct",
            "user": "usr",
            "credentials_dir": str(missing_dir),
            # Default filename → will trigger FileNotFoundError on read.
        },
        None,
    )
    engine = SnowparkEngine(args={"connection": "sf_bad_path"})
    result = engine.test()
    assert result.ok is False
    assert result.code == "auth"
    assert "credentials file unavailable" in result.message.lower()


# ---------------------------------------------------------------------------
# Field spec + capability flags (regression guards)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_field_spec_declares_connection_arg() -> None:
    """The web-safe field spec advertises the new ``connection`` arg."""
    from ovid2.config.engines import engine_field_spec

    spec = engine_field_spec("snowpark")
    arg_names = [f["name"] for f in spec["args"]]
    assert "connection" in arg_names
    connection_field = next(f for f in spec["args"] if f["name"] == "connection")
    assert connection_field["required"] is False
    # 'account' + 'user' are now optional too (only required when
    # 'connection' is blank).
    account_field = next(f for f in spec["args"] if f["name"] == "account")
    user_field = next(f for f in spec["args"] if f["name"] == "user")
    assert account_field["required"] is False
    assert user_field["required"] is False


@pytest.mark.unit
def test_snowpark_still_advertises_reduced_capabilities() -> None:
    """The new credential path must not change the engine's capability flags."""
    engine = SnowparkEngine(args={"connection": "irrelevant"})
    assert engine.family is EngineFamily.SNOWPARK
    assert engine.session_is_spark is False
    assert engine.supports_pandas_udf is False
    assert engine.supports_broadcast_hint is False
    assert engine.supports_input_file_name is False
    assert engine.supports_local_files is False


# ---------------------------------------------------------------------------
# prepare_workspace — bind a writable namespace for temp objects
# ---------------------------------------------------------------------------


class _RecordingNative:
    """Records SQL statements issued against the session."""

    def __init__(self) -> None:
        self.sql_calls: list[str] = []

    def sql(self, query: str) -> Any:
        self.sql_calls.append(query)
        result = MagicMock()
        result.collect.return_value = []
        return result


@pytest.mark.unit
def test_prepare_workspace_binds_writable_db_and_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``prepare_workspace`` issues USE DATABASE + USE SCHEMA for the staging schema."""
    engine = SnowparkEngine(args={"connection": "irrelevant"})
    native = _RecordingNative()
    engine._native = native  # type: ignore[assignment]

    # Stub the writable-schema resolver so the test needs no Snowflake.
    import ovid2.drivers.snowflake.staging.disc as disc_mod

    monkeypatch.setattr(
        disc_mod,
        "_ensure_writable_staging_schema",
        lambda _n, fallback_databases=(): "OMOP_OUT.OVID2_SNOWPARK_STAGING",
    )

    engine.prepare_workspace()

    assert "USE DATABASE OMOP_OUT" in native.sql_calls
    assert "USE SCHEMA OMOP_OUT.OVID2_SNOWPARK_STAGING" in native.sql_calls


@pytest.mark.unit
def test_prepare_workspace_propagates_unwritable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no writable schema resolves, the RuntimeError propagates."""
    engine = SnowparkEngine(args={"connection": "irrelevant"})
    engine._native = _RecordingNative()  # type: ignore[assignment]

    import ovid2.drivers.snowflake.staging.disc as disc_mod

    def _boom(_n: Any, fallback_databases: Any = ()) -> str:
        raise RuntimeError("no writable current database")

    monkeypatch.setattr(disc_mod, "_ensure_writable_staging_schema", _boom)

    with pytest.raises(RuntimeError, match="no writable current database"):
        engine.prepare_workspace()


@pytest.mark.unit
def test_prepare_workspace_threads_and_remembers_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback databases reach the resolver and persist for the per-table re-bind."""
    engine = SnowparkEngine(args={"connection": "irrelevant"})
    engine._native = _RecordingNative()  # type: ignore[assignment]

    import ovid2.drivers.snowflake.staging.disc as disc_mod

    seen: list[tuple[str, ...]] = []

    def _resolve(_n: Any, fallback_databases: Any = ()) -> str:
        seen.append(tuple(fallback_databases))
        return "CHAR_X.OVID2_SNOWPARK_STAGING"

    monkeypatch.setattr(disc_mod, "_ensure_writable_staging_schema", _resolve)

    engine.prepare_workspace(["CHAR_X", "CHAR_Y"])
    engine.prepare_workspace()  # core/pipeline.py re-bind: no argument

    assert seen == [("CHAR_X", "CHAR_Y"), ("CHAR_X", "CHAR_Y")]


@pytest.mark.unit
def test_base_engine_prepare_workspace_is_noop() -> None:
    """The default (Spark) prepare_workspace does nothing and returns None."""
    from ovid2.core.engines.pyspark import PySparkEngine

    engine = PySparkEngine()
    assert engine.prepare_workspace() is None
    assert engine.prepare_workspace(["ANY_DB"]) is None


# ---------------------------------------------------------------------------
# checkpoint — materialisation boundary (Change B)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_base_engine_checkpoint_caches_dataframe() -> None:
    """The default (Spark) checkpoint marks the DataFrame for caching."""
    from ovid2.core.engines.pyspark import PySparkEngine

    cached = object()

    class _FakeDF:
        def cache(self) -> Any:
            return cached

    engine = PySparkEngine()
    result = engine.checkpoint(_FakeDF(), "common_measurement")
    assert result is cached


@pytest.mark.unit
def test_snowpark_checkpoint_writes_transient_table_and_returns_handle() -> None:
    """Snowpark checkpoint writes a transient table and returns a fresh handle."""
    handle = object()
    calls: dict[str, Any] = {}

    class _FakeWriter:
        def mode(self, m: str) -> _FakeWriter:
            calls["mode"] = m
            return self

        def save_as_table(self, name: str, table_type: str | None = None) -> None:
            calls["name"] = name
            calls["table_type"] = table_type

    class _FakeDF:
        write = _FakeWriter()

    class _FakeNative:
        def table(self, name: str) -> Any:
            calls["table_read"] = name
            return handle

    engine = SnowparkEngine(args={"account": "a", "user": "u"}, secrets={"password": "p"})
    engine._native = _FakeNative()  # type: ignore[assignment]
    engine._session = object()  # type: ignore[assignment]  # short-circuit get_session

    result = engine.checkpoint(_FakeDF(), "common_measurement")

    assert result is handle
    assert calls["mode"] == "overwrite"
    assert calls["table_type"] == "transient"
    # Name is sanitised + prefixed + uppercased.
    assert calls["name"] == "OVID2_CKPT_COMMON_MEASUREMENT"
    # The returned handle reads back the same transient table.
    assert calls["table_read"] == "OVID2_CKPT_COMMON_MEASUREMENT"


@pytest.mark.unit
def test_snowpark_checkpoint_sanitises_name() -> None:
    """Non-alphanumeric characters in the name collapse to underscores."""
    calls: dict[str, Any] = {}

    class _FakeWriter:
        def mode(self, m: str) -> _FakeWriter:
            return self

        def save_as_table(self, name: str, table_type: str | None = None) -> None:
            calls["name"] = name

    class _FakeDF:
        write = _FakeWriter()

    class _FakeNative:
        def table(self, name: str) -> Any:
            return object()

    engine = SnowparkEngine(args={"account": "a", "user": "u"}, secrets={"password": "p"})
    engine._native = _FakeNative()  # type: ignore[assignment]
    engine._session = object()  # type: ignore[assignment]  # short-circuit get_session
    engine.checkpoint(_FakeDF(), "common/observation-99")
    assert calls["name"] == "OVID2_CKPT_COMMON_OBSERVATION_99"


# ---------------------------------------------------------------------------
# cancel_running_queries (SIGTERM / SIGINT cooperative cancel)
# ---------------------------------------------------------------------------


class _FakeCollectable:
    def __init__(self, sink: dict[str, Any]) -> None:
        self._sink = sink

    def collect(self) -> list[Any]:
        self._sink["collected"] = True
        return []


class _FakeCancelSession:
    def __init__(self, session_id: Any = 12345, *, raise_on_sql: bool = False) -> None:
        self.session_id = session_id
        self.sql_calls: list[str] = []
        self._raise_on_sql = raise_on_sql
        self.sink: dict[str, Any] = {}

    def sql(self, query: str) -> _FakeCollectable:
        self.sql_calls.append(query)
        if self._raise_on_sql:
            raise RuntimeError("boom")
        return _FakeCollectable(self.sink)


@pytest.mark.unit
def test_cancel_running_queries_issues_cancel_all_with_session_id() -> None:
    """A live session id yields SYSTEM$CANCEL_ALL_QUERIES(<id>)."""
    engine = SnowparkEngine(args={"account": "a", "user": "u"}, secrets={"password": "p"})
    fake = _FakeCancelSession(session_id=98765)
    engine._native = fake  # type: ignore[assignment]
    engine.cancel_running_queries()
    assert fake.sql_calls, "expected a cancel SQL statement"
    assert "SYSTEM$CANCEL_ALL_QUERIES(98765)" in fake.sql_calls[0]
    assert fake.sink.get("collected") is True


@pytest.mark.unit
def test_cancel_running_queries_noop_without_session() -> None:
    """No live session → no SQL, no error."""
    engine = SnowparkEngine(args={"account": "a", "user": "u"}, secrets={"password": "p"})
    engine._native = None  # type: ignore[assignment]
    engine.cancel_running_queries()  # must not raise


@pytest.mark.unit
def test_cancel_running_queries_falls_back_to_connection_session_id() -> None:
    """When Session lacks session_id, use connection.session_id."""

    class _ConnOnly:
        def __init__(self) -> None:
            self.session_id = None
            self.connection = type("C", (), {"session_id": 4242})()
            self.sql_calls: list[str] = []

        def sql(self, query: str) -> _FakeCollectable:
            self.sql_calls.append(query)
            return _FakeCollectable({})

    engine = SnowparkEngine(args={"account": "a", "user": "u"}, secrets={"password": "p"})
    fake = _ConnOnly()
    engine._native = fake  # type: ignore[assignment]
    engine.cancel_running_queries()
    assert "SYSTEM$CANCEL_ALL_QUERIES(4242)" in fake.sql_calls[0]


@pytest.mark.unit
def test_cancel_running_queries_skips_when_no_session_id() -> None:
    """Unresolvable session id → skip cleanly (no SQL)."""

    class _NoId:
        session_id = None
        connection = None

        def __init__(self) -> None:
            self.sql_calls: list[str] = []

        def sql(self, query: str) -> _FakeCollectable:  # pragma: no cover
            self.sql_calls.append(query)
            return _FakeCollectable({})

    engine = SnowparkEngine(args={"account": "a", "user": "u"}, secrets={"password": "p"})
    fake = _NoId()
    engine._native = fake  # type: ignore[assignment]
    engine.cancel_running_queries()
    assert fake.sql_calls == []


@pytest.mark.unit
def test_cancel_running_queries_swallows_sql_errors() -> None:
    """A Snowflake error during cancel is swallowed, not raised."""
    engine = SnowparkEngine(args={"account": "a", "user": "u"}, secrets={"password": "p"})
    fake = _FakeCancelSession(session_id=1, raise_on_sql=True)
    engine._native = fake  # type: ignore[assignment]
    engine.cancel_running_queries()  # must not raise


@pytest.mark.unit
def test_base_engine_cancel_running_queries_is_noop() -> None:
    """The base engine default cancel is a no-op (Spark family)."""
    from ovid2.core.engines.pyspark import PySparkEngine

    engine = PySparkEngine(args={})
    # Should not raise even without a session.
    engine.cancel_running_queries()


# ---------------------------------------------------------------------------
# Staging cleanups run over the live session, before close()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stop_session_runs_staging_cleanups_before_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ovid2.core.engines import stager as stager_mod

    monkeypatch.setattr("atexit.register", lambda fn: fn)
    order: list[str] = []

    class _Native:
        def close(self) -> None:
            order.append("close")

    native = _Native()
    engine = SnowparkEngine(args={"connection": "irrelevant"})
    engine._native = native  # type: ignore[assignment]
    once = stager_mod.register_session_cleanup(native, lambda: order.append("cleanup"))

    engine.stop_session()
    once()  # the atexit backstop firing later must be a no-op

    assert order == ["cleanup", "close"]
