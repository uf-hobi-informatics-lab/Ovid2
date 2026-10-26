"""Unit tests for :mod:`ovid2.drivers.s3.probe`.

Uses ``sys.modules`` stubbing to fake :mod:`boto3` so we can exercise
the branches without hitting the network:

- ``boto3`` not installed → ``code='skipped'``.
- ``bucket_default`` set → ``HEAD s3://<bucket>`` succeeds.
- No ``bucket_default`` → falls back to ``list_buckets()``.
- ``head_bucket`` raises → ``code='unreachable'``.
- Credentials + region correctly threaded through to the session/client.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

from ovid2.drivers.base import ProbeResult
from ovid2.drivers.s3.probe import probe_auth_from_config

# ---------------------------------------------------------------------------
# boto3 fake
# ---------------------------------------------------------------------------


class _FakeClient:
    """Records the calls the probe makes."""

    def __init__(
        self,
        *,
        head_raises: Exception | None = None,
        list_raises: Exception | None = None,
    ) -> None:
        self.head_calls: list[dict[str, Any]] = []
        self.list_calls: list[tuple[Any, ...]] = []
        self._head_raises = head_raises
        self._list_raises = list_raises

    def head_bucket(self, **kw: Any) -> dict[str, Any]:
        self.head_calls.append(kw)
        if self._head_raises:
            raise self._head_raises
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def list_buckets(self, *a: Any, **kw: Any) -> dict[str, Any]:
        self.list_calls.append(a)
        if self._list_raises:
            raise self._list_raises
        return {"Buckets": []}


class _FakeSession:
    def __init__(self, **kwargs: Any) -> None:
        self.session_kwargs = kwargs
        self.client_kwargs: dict[str, Any] = {}
        self._client = _FakeClient()

    def client(self, service: str, **kwargs: Any) -> _FakeClient:
        self.client_kwargs = kwargs
        return self._client


class _FakeSessionModule:
    """Stub for ``boto3.session``."""

    Session = _FakeSession


class _FakeBoto3:
    """The parts of :mod:`boto3` the probe reaches for."""

    session = _FakeSessionModule


@pytest.fixture()
def fake_boto3(monkeypatch: pytest.MonkeyPatch) -> _FakeBoto3:
    """Install a fake ``boto3`` in :data:`sys.modules` for one test."""
    mod = ModuleType("boto3")
    session_mod = ModuleType("boto3.session")
    session_mod.Session = _FakeSession  # type: ignore[attr-defined]
    mod.session = session_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", mod)
    monkeypatch.setitem(sys.modules, "boto3.session", session_mod)
    return _FakeBoto3()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBoto3Missing:
    """When boto3 is not available, return ``skipped`` cleanly."""

    def test_skipped_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force the import to fail even if boto3 is installed.
        monkeypatch.setitem(sys.modules, "boto3", None)
        r = probe_auth_from_config({}, {})
        assert isinstance(r, ProbeResult)
        assert r.ok is False
        assert r.code == "skipped"
        assert r.capability == "auth"


class TestHeadBucketSuccess:
    """When ``bucket_default`` is set, we probe via HEAD."""

    def test_ok(self, fake_boto3: _FakeBoto3) -> None:
        r = probe_auth_from_config(
            {"region": "us-east-1", "bucket_default": "my-bucket"},
            {"aws_access_key_id": "A", "aws_secret_access_key": "S"},
        )
        assert r.ok is True
        assert r.code == "ok"
        assert "HEAD s3://my-bucket" in r.message
        assert r.detail == {"bucket": "my-bucket"}
        assert r.capability == "auth"


class TestListBucketsFallback:
    """No ``bucket_default`` → probe via list_buckets."""

    def test_ok(self, fake_boto3: _FakeBoto3) -> None:
        r = probe_auth_from_config(
            {"region": "us-east-1"},
            {"aws_access_key_id": "A", "aws_secret_access_key": "S"},
        )
        assert r.ok is True
        assert r.code == "ok"
        assert "list_buckets" in r.message


class TestHeadBucketFailure:
    """When the probe raises, downgrade to ``unreachable``."""

    def test_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Install a fake boto3 whose head_bucket raises.
        boom = RuntimeError("access denied")

        class FailingSession:
            def __init__(self, **kw: Any) -> None:
                self._client = _FakeClient(head_raises=boom)

            def client(self, service: str, **kw: Any) -> _FakeClient:
                return self._client

        mod = ModuleType("boto3")
        session_mod = ModuleType("boto3.session")
        session_mod.Session = FailingSession  # type: ignore[attr-defined]
        mod.session = session_mod  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "boto3", mod)
        monkeypatch.setitem(sys.modules, "boto3.session", session_mod)

        r = probe_auth_from_config(
            {"bucket_default": "b"},
            {"aws_access_key_id": "A", "aws_secret_access_key": "S"},
        )
        assert r.ok is False
        assert r.code == "unreachable"
        assert "access denied" in r.message


class TestCredentialThreading:
    """The session and client kwargs must match the input triple."""

    def test_full_triple_and_endpoint_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Capture sessions in a shared list so we can inspect kwargs.
        constructed: list[_FakeSession] = []

        class RecordingSession(_FakeSession):
            def __init__(self, **kw: Any) -> None:
                super().__init__(**kw)
                constructed.append(self)

        mod = ModuleType("boto3")
        session_mod = ModuleType("boto3.session")
        session_mod.Session = RecordingSession  # type: ignore[attr-defined]
        mod.session = session_mod  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "boto3", mod)
        monkeypatch.setitem(sys.modules, "boto3.session", session_mod)

        probe_auth_from_config(
            {
                "region": "us-west-2",
                "endpoint_url": "https://minio.example.com",
                "bucket_default": "b",
            },
            {
                "aws_access_key_id": "A",
                "aws_secret_access_key": "S",
                "aws_session_token": "T",
            },
        )

        assert len(constructed) == 1
        sess = constructed[0]
        assert sess.session_kwargs == {
            "aws_access_key_id": "A",
            "aws_secret_access_key": "S",
            "aws_session_token": "T",
            "region_name": "us-west-2",
        }
        assert sess.client_kwargs == {"endpoint_url": "https://minio.example.com"}

    def test_default_chain_omits_credentials(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        constructed: list[_FakeSession] = []

        class RecordingSession(_FakeSession):
            def __init__(self, **kw: Any) -> None:
                super().__init__(**kw)
                constructed.append(self)

        mod = ModuleType("boto3")
        session_mod = ModuleType("boto3.session")
        session_mod.Session = RecordingSession  # type: ignore[attr-defined]
        mod.session = session_mod  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "boto3", mod)
        monkeypatch.setitem(sys.modules, "boto3.session", session_mod)

        probe_auth_from_config({"region": "us-east-1"}, {})

        assert len(constructed) == 1
        # No credentials were supplied → session kwargs carry only region.
        assert "aws_access_key_id" not in constructed[0].session_kwargs
        assert "aws_secret_access_key" not in constructed[0].session_kwargs


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run with pytest.")
