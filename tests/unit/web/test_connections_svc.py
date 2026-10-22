"""Tests for the connections service (W7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ovid2.web.models import ConnectionWrite
from ovid2.web.services import connections_svc


@pytest.mark.unit
def test_list_types_returns_every_registered_connector(isolated_xdg: Path) -> None:
    specs = connections_svc.list_connection_types()
    names = {s.type for s in specs}
    assert {
        "disc",
        "snowflake",
        "s3",
        "azure_blob",
        "gcs",
        "databricks",
        "postgres",
        "mssql",
    } <= names


@pytest.mark.unit
def test_snowflake_field_spec_advertises_file_location_shape() -> None:
    """The Snowflake spec captures file locations, not credential values.

    The panel deliberately does not accept a PEM body or a raw
    passphrase — those files stay on disk, managed by ops. The
    spec must therefore:

    - Expose ``credentials_dir``, ``private_key_file``, and
      ``private_key_passphrase_file`` as **non-secret** args (their
      values are directory / filename strings).
    - **Not** expose ``private_key`` or ``private_key_passphrase`` as
      secrets (either the old inline shape or the older
      path-in-secret shape).
    - Keep ``password`` as an optional secret for password-auth
      accounts.
    """
    from ovid2.connections.registry import connection_field_spec

    spec = connection_field_spec("snowflake")
    arg_names = {f["name"] for f in spec["args"]}
    secret_names = {f["name"] for f in spec["secrets"]}

    assert {"credentials_dir", "private_key_file", "private_key_passphrase_file"} <= arg_names
    assert "private_key" not in secret_names
    assert "private_key_passphrase" not in secret_names
    assert secret_names == {"password"}


@pytest.mark.unit
def test_write_and_read_disc_connection(isolated_xdg: Path) -> None:
    result = connections_svc.write_connection(
        "c_disc",
        ConnectionWrite(type="disc", args={"host_path": str(isolated_xdg)}),
        actor="test",
        create_only=True,
    )
    assert result.action == "created"
    detail = connections_svc.get_connection("c_disc")
    assert detail is not None
    assert detail.type == "disc"
    assert detail.args["host_path"] == str(isolated_xdg)
    assert detail.secret_ref is None


@pytest.mark.unit
def test_write_with_secrets_writes_secret_file(isolated_xdg: Path) -> None:
    connections_svc.write_connection(
        "c_sf",
        ConnectionWrite(
            type="snowflake",
            args={"account": "acc", "user": "u"},
            secrets={"password": "hunter2"},
        ),
        actor="test",
        create_only=True,
    )
    secrets_root = isolated_xdg / "config" / "ovid2" / "secrets.d"
    secret_file = secrets_root / "conn_c_sf.json"
    assert secret_file.is_file()
    # 0o600 enforced
    mode = secret_file.stat().st_mode & 0o777
    assert mode == 0o600
    payload = json.loads(secret_file.read_text())
    assert payload["password"] == "hunter2"
    # The connection JSON itself never has the secret inline.
    conn_file = isolated_xdg / "config" / "ovid2" / "connections" / "c_sf.json"
    assert "password" not in conn_file.read_text()


@pytest.mark.unit
def test_masked_sentinel_preserves_secret(isolated_xdg: Path) -> None:
    connections_svc.write_connection(
        "c_sf",
        ConnectionWrite(
            type="snowflake",
            args={"account": "a", "user": "u"},
            secrets={"password": "hunter2"},
        ),
        actor="test",
        create_only=True,
    )
    connections_svc.write_connection(
        "c_sf",
        ConnectionWrite(
            type="snowflake",
            args={"account": "different", "user": "u"},
            secrets={"password": "***"},
        ),
        actor="test",
    )
    secret_file = isolated_xdg / "config" / "ovid2" / "secrets.d" / "conn_c_sf.json"
    assert json.loads(secret_file.read_text())["password"] == "hunter2"


@pytest.mark.unit
def test_reject_inline_secret_in_args(isolated_xdg: Path) -> None:
    with pytest.raises(connections_svc.ConnectionError, match="secrets"):
        connections_svc.write_connection(
            "c_bad",
            ConnectionWrite(
                type="snowflake",
                args={"account": "a", "user": "u", "password": "hunter2"},
            ),
            actor="test",
            create_only=True,
        )


@pytest.mark.unit
def test_reject_unknown_field(isolated_xdg: Path) -> None:
    with pytest.raises(connections_svc.ConnectionError, match="Unknown"):
        connections_svc.write_connection(
            "c_bad",
            ConnectionWrite(
                type="disc",
                args={"host_path": "/tmp", "totally_made_up_key": "x"},
            ),
            actor="test",
            create_only=True,
        )


@pytest.mark.unit
def test_test_probe_disc_ok(isolated_xdg: Path, tmp_path: Path) -> None:
    root = tmp_path / "existing"
    root.mkdir()
    connections_svc.write_connection(
        "c_disc",
        ConnectionWrite(type="disc", args={"host_path": str(root)}),
        actor="test",
        create_only=True,
    )
    report = connections_svc.test_connection("c_disc")
    assert report.ok is True
    assert report.code == "ok"


@pytest.mark.unit
def test_test_probe_disc_missing_dir(isolated_xdg: Path, tmp_path: Path) -> None:
    connections_svc.write_connection(
        "c_disc",
        ConnectionWrite(type="disc", args={"host_path": str(tmp_path / "does_not_exist")}),
        actor="test",
        create_only=True,
    )
    report = connections_svc.test_connection("c_disc")
    assert report.ok is False
    assert report.code == "unreachable"


@pytest.mark.unit
def test_delete_connection(isolated_xdg: Path, tmp_path: Path) -> None:
    connections_svc.write_connection(
        "c_disc",
        ConnectionWrite(type="disc", args={"host_path": str(tmp_path)}),
        actor="test",
        create_only=True,
    )
    connections_svc.delete_connection("c_disc", actor="test")
    assert connections_svc.get_connection("c_disc") is None


@pytest.mark.unit
def test_create_only_conflict(isolated_xdg: Path, tmp_path: Path) -> None:
    connections_svc.write_connection(
        "c_disc",
        ConnectionWrite(type="disc", args={"host_path": str(tmp_path)}),
        actor="test",
        create_only=True,
    )
    with pytest.raises(connections_svc.ConnectionError, match="already exists"):
        connections_svc.write_connection(
            "c_disc",
            ConnectionWrite(type="disc", args={"host_path": str(tmp_path)}),
            actor="test",
            create_only=True,
        )


@pytest.mark.unit
def test_probe_deadline_returns_timeout_code(
    isolated_xdg: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that outlives the deadline must not wedge the caller.

    Regression: on 2026-08-03 an Azure Blob probe against a
    non-resolvable account name took 85 s because the Azure SDK's
    default retry policy runs 10 exponentially-backed-off retries.
    The synchronous ``POST /connections/{name}/test`` route
    inherited that latency and the browser appeared frozen with a
    'Testing…' banner. The Azure connector now fails fast at the
    SDK level, but the service layer must also enforce a hard
    ceiling so a *future* misbehaving connector cannot recur this
    incident.

    Probing now runs in an executor subprocess (harness/API/executor
    trust model), so the deadline is enforced by ``run_tool_json``'s
    subprocess timeout. A timeout surfaces as a ``BrokerError``, which
    the service maps to an ``unreachable`` report.
    """
    connections_svc.write_connection(
        "c_slow",
        ConnectionWrite(type="disc", args={"host_path": str(tmp_path)}),
        actor="test",
        create_only=True,
    )

    from ovid2.web.services._broker import BrokerError

    def _timeout(tool_args: object, **kw: object) -> dict:
        raise BrokerError("Operation timed out after 0s")

    monkeypatch.setattr(connections_svc, "run_tool_json", _timeout)

    report = connections_svc.test_connection("c_slow")

    assert report.ok is False
    assert report.code == "unreachable"
    assert "timed out" in report.message.lower()


@pytest.mark.unit
def test_probe_that_raises_reports_unreachable_not_deadline(
    isolated_xdg: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe failure surfaced by the broker maps to a clean report."""
    connections_svc.write_connection(
        "c_bad",
        ConnectionWrite(type="disc", args={"host_path": str(tmp_path)}),
        actor="test",
        create_only=True,
    )

    # The executor subprocess reports the failure as a JSON result with
    # ok=False; the service must pass it through faithfully.
    def _failed(tool_args: object, **kw: object) -> dict:
        return {
            "ok": False,
            "code": "unreachable",
            "message": "Probe raised RuntimeError: kaboom",
            "detail": {},
        }

    monkeypatch.setattr(connections_svc, "run_tool_json", _failed)
    report = connections_svc.test_connection("c_bad")
    assert report.ok is False
    assert report.code == "unreachable"
    assert "RuntimeError" in report.message


# ---------------------------------------------------------------------------
# W-Azure follow-up: managed-identity note + auth_mode surfacing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_azure_blob_field_spec_has_managed_identity_note() -> None:
    """The catalogue publishes the managed-identity guidance note."""
    from ovid2.connections.registry import connection_field_spec

    spec = connection_field_spec("azure_blob")
    assert spec.get("note")
    assert "managed identity" in str(spec["note"]).lower()


@pytest.mark.unit
def test_disc_field_spec_has_no_note() -> None:
    """Connectors that do not publish a note return ``None`` for it.

    NOTE (disc-mount phase): ``disc`` grew a note when the widened
    schema landed; postgres remains a stable example of a connector
    with no top-level guidance.
    """
    from ovid2.connections.registry import connection_field_spec

    spec = connection_field_spec("postgres")
    assert spec.get("note") is None


@pytest.mark.unit
def test_list_connection_types_forwards_note() -> None:
    """The service passes the connector-level note through unchanged."""
    specs = {s.type: s for s in connections_svc.list_connection_types()}
    assert specs["azure_blob"].note
    assert specs["postgres"].note is None


@pytest.mark.unit
def test_get_connection_reports_managed_identity_auth_mode(
    isolated_xdg: Path,
) -> None:
    """An azure_blob connection with no secrets reports managed identity."""
    connections_svc.write_connection(
        "az_mi",
        ConnectionWrite(
            type="azure_blob",
            args={"account_name": "acct"},
        ),
        actor="test",
        create_only=True,
    )
    detail = connections_svc.get_connection("az_mi")
    assert detail is not None
    assert detail.auth_mode == "default_chain"


@pytest.mark.unit
def test_get_connection_reports_service_principal_auth_mode(
    isolated_xdg: Path,
) -> None:
    """Populated SPN secrets flip the reported mode."""
    connections_svc.write_connection(
        "az_spn",
        ConnectionWrite(
            type="azure_blob",
            args={"account_name": "acct"},
            secrets={"tenant_id": "t", "client_id": "c", "client_secret": "s"},
        ),
        actor="test",
        create_only=True,
    )
    detail = connections_svc.get_connection("az_spn")
    assert detail is not None
    assert detail.auth_mode == "service_principal"


@pytest.mark.unit
def test_non_azure_connection_has_no_auth_mode(isolated_xdg: Path) -> None:
    """Only azure_blob currently publishes a resolved auth_mode."""
    connections_svc.write_connection(
        "d",
        ConnectionWrite(type="disc", args={"host_path": str(isolated_xdg)}),
        actor="test",
        create_only=True,
    )
    detail = connections_svc.get_connection("d")
    assert detail is not None
    assert detail.auth_mode is None


# ---------------------------------------------------------------------------
# D4: driver-based probe wrapper
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_test_connection_via_driver_ok(
    isolated_xdg: Path,
    tmp_path: Path,
) -> None:
    """Driver-based probe returns identical shape for a valid disc conn."""
    root = tmp_path / "d4_ok"
    root.mkdir()
    connections_svc.write_connection(
        "c_d4",
        ConnectionWrite(type="disc", args={"host_path": str(root)}),
        actor="test",
        create_only=True,
    )
    report = connections_svc.test_connection_via_driver("c_d4")
    assert report.ok is True
    assert report.code == "ok"


@pytest.mark.unit
def test_test_connection_via_driver_matches_legacy(
    isolated_xdg: Path,
    tmp_path: Path,
) -> None:
    """Driver + legacy probes must produce byte-identical results today.

    ``LegacyDriverAdapter.probe_auth`` internally calls
    ``BaseConnection.test``, so the two entry points must agree.
    This lock-step check prevents a future driver-side change from
    silently diverging.
    """
    root = tmp_path / "d4_parity"
    root.mkdir()
    connections_svc.write_connection(
        "c_parity",
        ConnectionWrite(type="disc", args={"host_path": str(root)}),
        actor="test",
        create_only=True,
    )
    legacy = connections_svc.test_connection("c_parity")
    driver = connections_svc.test_connection_via_driver("c_parity")
    assert legacy.ok == driver.ok
    assert legacy.code == driver.code
    assert legacy.message == driver.message


@pytest.mark.unit
def test_test_connection_via_driver_missing_returns_config(
    isolated_xdg: Path,
) -> None:
    """A connection JSON with an unregistered type is reported cleanly."""
    conn_dir = isolated_xdg / "config" / "ovid2" / "connections"
    conn_dir.mkdir(parents=True, exist_ok=True)
    bad = conn_dir / "c_bogus.json"
    bad.write_text('{"type": "nope_engine", "args": {}}', encoding="utf-8")
    report = connections_svc.test_connection_via_driver("c_bogus")
    assert report.ok is False
    assert report.code == "config"


@pytest.mark.unit
def test_test_connection_via_driver_unreachable_dir(
    isolated_xdg: Path,
    tmp_path: Path,
) -> None:
    """A disc pointing at a missing directory reports unreachable."""
    connections_svc.write_connection(
        "c_missing",
        ConnectionWrite(
            type="disc",
            args={"host_path": str(tmp_path / "ghost")},
        ),
        actor="test",
        create_only=True,
    )
    report = connections_svc.test_connection_via_driver("c_missing")
    assert report.ok is False
    assert report.code == "unreachable"
