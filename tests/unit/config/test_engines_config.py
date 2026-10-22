"""Unit tests for :mod:`ovid2.config.engines` — web-safe engine loader."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ovid2.config import engines as _engines


@pytest.fixture()
def engines_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the engines search path to a tmp directory."""
    root = tmp_path / "engines"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OVID2_ENGINES_DIR", str(root))
    # Kill the other layers so tests do not see host artefacts.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return root


@pytest.mark.unit
def test_list_engine_types_returns_registered_three() -> None:
    assert _engines.list_engine_types() == [
        "databricks_spark",
        "pyspark",
        "snowpark",
    ]


@pytest.mark.unit
def test_field_spec_shape_pyspark() -> None:
    spec = _engines.engine_field_spec("pyspark")
    assert set(spec.keys()) == {"args", "secrets"}
    assert spec["secrets"] == []  # pyspark has no credentials
    arg_names = [f["name"] for f in spec["args"]]
    assert "app_name" in arg_names
    assert "spark.executor.memory" in arg_names


@pytest.mark.unit
def test_field_spec_shape_snowpark_has_secrets() -> None:
    spec = _engines.engine_field_spec("snowpark")
    secret_names = [f["name"] for f in spec["secrets"]]
    assert "password" in secret_names
    assert "private_key" in secret_names


@pytest.mark.unit
def test_field_spec_rejects_unknown_type() -> None:
    with pytest.raises(KeyError):
        _engines.engine_field_spec("no_such_engine")


@pytest.mark.unit
def test_engines_dir_uses_xdg_config_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("OVID2_ENGINES_DIR", raising=False)
    result = _engines.engines_dir()
    assert result == tmp_path / "xdg" / "ovid2" / "engines"


@pytest.mark.unit
def test_locate_engine_returns_none_when_missing(engines_root: Path) -> None:
    assert _engines.locate_engine("nothing_here") is None


@pytest.mark.unit
def test_list_available_engines_ignores_sample_and_nondotjson(
    engines_root: Path,
) -> None:
    (engines_root / "real.json").write_text('{"type":"pyspark","args":{}}')
    (engines_root / "template_sample.json").write_text('{"type":"pyspark","args":{}}')
    (engines_root / "notes.txt").write_text("ignore me")
    assert _engines.list_available_engines() == ["real"]


@pytest.mark.unit
def test_load_engine_config_happy_path(engines_root: Path) -> None:
    (engines_root / "pyspark_test.json").write_text(
        json.dumps({"type": "pyspark", "args": {"app_name": "T"}})
    )
    config = _engines.load_engine_config("pyspark_test")
    assert config["type"] == "pyspark"
    assert config["args"]["app_name"] == "T"


@pytest.mark.unit
def test_load_engine_config_rejects_unknown_type(engines_root: Path) -> None:
    (engines_root / "weird.json").write_text(
        json.dumps({"type": "no_such_engine", "args": {}})
    )
    with pytest.raises(ValueError, match="Unknown engine type"):
        _engines.load_engine_config("weird")


@pytest.mark.unit
def test_load_engine_config_missing_type(engines_root: Path) -> None:
    (engines_root / "no_type.json").write_text(json.dumps({"args": {}}))
    with pytest.raises(ValueError, match="missing 'type'"):
        _engines.load_engine_config("no_type")


@pytest.mark.unit
def test_load_engine_config_rejects_non_object_args(engines_root: Path) -> None:
    (engines_root / "bad_args.json").write_text(
        json.dumps({"type": "pyspark", "args": ["not", "a", "dict"]})
    )
    with pytest.raises(ValueError, match="non-object 'args'"):
        _engines.load_engine_config("bad_args")


@pytest.mark.unit
def test_load_engine_config_refuses_inline_secret(engines_root: Path) -> None:
    """databricks_spark 'token' living in args must be rejected."""
    (engines_root / "leaky.json").write_text(
        json.dumps({
            "type": "databricks_spark",
            "args": {"workspace_url": "https://x", "token": "SECRET"},
        })
    )
    with pytest.raises(ValueError, match="inline in 'args'"):
        _engines.load_engine_config("leaky")


@pytest.mark.unit
def test_load_engine_config_not_found_raises(engines_root: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _engines.load_engine_config("nope")


@pytest.mark.unit
def test_resolve_secrets_empty_when_no_secret_ref() -> None:
    assert _engines.resolve_secrets({"type": "pyspark", "args": {}}) == {}


@pytest.mark.unit
def test_resolve_secrets_reads_referenced_file(
    engines_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets_dir = tmp_path / "xdg" / "ovid2" / "secrets.d"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    secret_path = secrets_dir / "engine_probe.json"
    secret_path.write_text(json.dumps({"token": "abc"}))
    os.chmod(secret_path, 0o600)
    result = _engines.resolve_secrets({"secret_ref": "engine_probe"})
    assert result == {"token": "abc"}
