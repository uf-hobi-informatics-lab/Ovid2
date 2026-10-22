"""JSON-driven engine loader (E1).

Resolves engine names into concrete :class:`ovid2.core.engines.base.BaseEngine`
instances and publishes the field-spec catalogue the web UI consumes.

**Split from the class-side registry** — this module is web-safe: it
does not import ``ovid2.core.engines.pyspark`` or any pyspark module.
That keeps ``src/ovid2/web/**`` free of pyspark-imports so
``tests/contract/test_web_no_pipeline_imports.py`` and the
implicit "web tier is install-light" invariant both hold.

Config file resolution order (identical to the connections loader):

1. ``$OVID2_ENGINES_DIR/<name>.json``
2. ``$XDG_CONFIG_HOME/ovid2/engines/<name>.json``
3. ``./engines/<name>.json`` (repo-local convenience for dev)
4. ``/etc/ovid2/engines/<name>.json``

Secret material is loaded via :mod:`ovid2.config.secrets` using the
optional ``secret_ref`` key on each engine file. Secrets stored inline
in the engine JSON are refused — the loader raises so mis-configured
files fail loudly rather than persisting plaintext credentials into
the config directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ovid2.config import secrets as _secrets_module

# Note: intentionally NOT importing ovid2.core.engines.* here. The web
# tier calls this module and must not force a pyspark dependency. The
# type name list is duplicated below and cross-checked by the
# ``tests/unit/core/engines/test_registry.py`` invariant test.

#: Type names known to the web layer. Mirrors
#: :data:`ovid2.core.engines.registry.ENGINE_TYPE_MAP`. A contract test
#: keeps the two in sync.
_KNOWN_TYPES: tuple[str, ...] = ("pyspark", "databricks_spark", "snowpark")

#: Type-name → engine family. Duplicates the ``family`` class attribute on
#: each :class:`~ovid2.core.engines.base.BaseEngine` subclass so the web
#: tier (and any other pyspark-free consumer) can consult it without
#: importing the ``ovid2.core.engines.*`` tree. The contract test
#: ``tests/unit/core/engines/test_registry.py`` keeps the two catalogues
#: in sync alongside :data:`_KNOWN_TYPES`.
_ENGINE_FAMILY: dict[str, str] = {
    "pyspark": "spark",
    "databricks_spark": "spark",
    "snowpark": "snowpark",
}


def engine_family(type_name: str) -> str | None:
    """Return the engine family (``'spark'`` / ``'snowpark'``) for a type.

    Web-safe helper used by the engine/driver compatibility check
    (:func:`ovid2.drivers.engine_compat.assert_engine_type_supports_drivers`).
    Returns ``None`` for unregistered type names — the caller decides
    whether to treat that as a hard error or skip the check.

    Args:
        type_name: One of :data:`_KNOWN_TYPES`.

    Returns:
        The family string, or ``None`` when unknown.
    """
    return _ENGINE_FAMILY.get(type_name)


# ---------------------------------------------------------------------------
# Field spec catalogue
# ---------------------------------------------------------------------------

_FIELD_SPECS: dict[str, dict[str, list[dict[str, Any]]]] = {
    "pyspark": {
        "args": [
            {
                "name": "app_name",
                "required": False,
                "default": "Ovid2 ETL",
                "help": "Spark application name shown in the Spark UI.",
            },
            {
                "name": "master_url",
                "required": False,
                "help": "Optional master URL override (e.g. spark://host:7077 or local[*]).",
            },
            {
                "name": "spark.driver.memory",
                "required": False,
                "default": "32g",
                "help": "Driver JVM heap size.",
            },
            {
                "name": "spark.driver.memoryOverhead",
                "required": False,
                "default": "4g",
                "help": "Driver off-heap overhead.",
            },
            {
                "name": "spark.driver.maxResultSize",
                "required": False,
                "default": "8g",
                "help": "Maximum collected result size on the driver.",
            },
            {
                "name": "spark.executor.memory",
                "required": False,
                "default": "48g",
                "help": "Executor JVM heap size.",
            },
            {
                "name": "spark.executor.memoryOverhead",
                "required": False,
                "default": "8g",
                "help": "Executor off-heap overhead.",
            },
            {
                "name": "spark.executor.cores",
                "required": False,
                "default": "8",
                "help": "Cores per executor.",
            },
            {
                "name": "spark.sql.shuffle.partitions",
                "required": False,
                "default": "200",
                "help": "Number of partitions for shuffles.",
            },
            {
                "name": "spark.default.parallelism",
                "required": False,
                "default": "200",
                "help": "Default parallelism for RDDs.",
            },
            {
                "name": "spark.sql.autoBroadcastJoinThreshold",
                "required": False,
                "default": "200m",
                "help": "Max broadcast table size.",
            },
        ],
        "secrets": [],
    },
    "databricks_spark": {
        "args": [
            {
                "name": "workspace_url",
                "required": True,
                "help": "Databricks workspace URL, e.g. https://<workspace>.cloud.databricks.com.",
            },
            {
                "name": "cluster_id",
                "required": False,
                "help": "Databricks cluster id (leave blank when using serverless / a SQL warehouse).",
            },
            {
                "name": "serverless",
                "required": False,
                "default": "false",
                "help": "'true' to route through serverless compute.",
            },
        ],
        "secrets": [
            {"name": "token", "required": True, "help": "Databricks personal access token."},
        ],
    },
    "snowpark": {
        "args": [
            {
                "name": "connection",
                "required": False,
                "help": "Name of an existing Snowflake connection to reuse "
                "for account + credentials (recommended). When set, "
                "'account' and 'user' below are only used as an override; "
                "leave the connection form's password/private key path "
                "as the source of truth. When blank, fill in 'account' + "
                "'user' + a snowpark secret_ref instead.",
            },
            {
                "name": "account",
                "required": False,
                "help": "Snowflake account identifier. Required when 'connection' "
                "is blank; ignored otherwise.",
            },
            {
                "name": "user",
                "required": False,
                "help": "Login user. Required when 'connection' is blank; ignored otherwise.",
            },
            {
                "name": "warehouse",
                "required": False,
                "help": "Default warehouse (optional). Overrides the connection's "
                "warehouse when both are set.",
            },
            {
                "name": "database",
                "required": False,
                "help": "Default database (optional). Overrides the connection's "
                "default database when both are set.",
            },
            {
                "name": "schema",
                "required": False,
                "help": "Default schema (optional). Overrides the connection's "
                "default schema when both are set.",
            },
            {
                "name": "role",
                "required": False,
                "help": "Default role (optional). Overrides the connection's "
                "role when both are set.",
            },
        ],
        "secrets": [
            {
                "name": "password",
                "required": False,
                "help": "Password (only for password-auth accounts; leave blank "
                "when using key-pair or a 'connection').",
            },
            {
                "name": "private_key",
                "required": False,
                "help": "RSA private key content (PEM). Leave blank when using "
                "password auth or a 'connection'.",
                "multiline": True,
            },
        ],
    },
}


def list_engine_types() -> list[str]:
    """Return the sorted list of registered engine type names."""
    return sorted(_KNOWN_TYPES)


def engine_field_spec(type_name: str) -> dict[str, list[dict[str, Any]]]:
    """Return the ``{args, secrets}`` field spec for an engine type.

    Args:
        type_name: One of the keys in :data:`_KNOWN_TYPES`.

    Returns:
        Deep-copied dict with two list keys ``args`` and ``secrets``.

    Raises:
        KeyError: When ``type_name`` is unknown.
    """
    if type_name not in _FIELD_SPECS:
        raise KeyError(
            f"No field spec for engine type '{type_name}'. "
            f"Registered: {sorted(_FIELD_SPECS.keys())}."
        )
    spec = _FIELD_SPECS[type_name]
    return {
        "args": [dict(f) for f in spec.get("args", [])],
        "secrets": [dict(f) for f in spec.get("secrets", [])],
    }


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _search_paths() -> list[Path]:
    """Return the ordered list of directories to search for engine JSONs."""
    paths: list[Path] = []
    override = os.environ.get("OVID2_ENGINES_DIR")
    if override:
        paths.append(Path(override))
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    paths.append(Path(xdg_cfg) / "ovid2" / "engines")
    paths.append(Path.cwd() / "engines")
    paths.append(Path("/etc/ovid2/engines"))
    return paths


def engines_dir() -> Path:
    """Return the user-scoped write target for engine files."""
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg_cfg) / "ovid2" / "engines"


def locate_engine(name: str) -> Path | None:
    """Return the on-disk path of an engine JSON, or ``None`` when absent."""
    for directory in _search_paths():
        candidate = directory / f"{name}.json"
        if candidate.is_file():
            return candidate
    return None


def list_available_engines() -> list[str]:
    """Return every engine name discoverable on the search path."""
    seen: set[str] = set()
    for directory in _search_paths():
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if entry.is_file() and entry.suffix == ".json" and not entry.stem.endswith("_sample"):
                seen.add(entry.stem)
    return sorted(seen)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_engine_config(name: str) -> dict[str, Any]:
    """Load a JSON engine config by name.

    Args:
        name: Filename stem (no ``.json``).

    Returns:
        Parsed dict guaranteed to have ``type`` and ``args``.

    Raises:
        FileNotFoundError: When no candidate directory contains the file.
        ValueError: When the JSON body is invalid or missing required keys.
    """
    for directory in _search_paths():
        candidate = directory / f"{name}.json"
        if candidate.is_file():
            with candidate.open(encoding="utf-8") as fh:
                config = json.load(fh)
            break
    else:
        tried = ", ".join(str(d) for d in _search_paths())
        raise FileNotFoundError(f"Engine config '{name}.json' not found in any of: {tried}")

    if not isinstance(config, dict):
        raise ValueError(f"Engine config '{name}' must be a JSON object.")
    if "type" not in config:
        raise ValueError(f"Engine config '{name}' missing 'type'.")
    if config["type"] not in _KNOWN_TYPES:
        raise ValueError(
            f"Unknown engine type '{config['type']}' in '{name}'. "
            f"Registered: {sorted(_KNOWN_TYPES)}."
        )
    config.setdefault("args", {})
    if not isinstance(config["args"], dict):
        raise ValueError(f"Engine '{name}' has non-object 'args'.")

    # Refuse inline secrets — the whole point of the split is that the
    # engine file itself never carries credential material.
    secret_field_names = {f["name"] for f in engine_field_spec(config["type"])["secrets"]}
    inline = sorted(set(config["args"]) & secret_field_names)
    if inline:
        raise ValueError(
            f"Engine '{name}' has secret fields inline in 'args': "
            f"{inline}. Move them into a secret file and set 'secret_ref'."
        )
    return config


def resolve_secrets(config: dict[str, Any]) -> dict[str, Any]:
    """Return the secret payload referenced by ``config['secret_ref']``.

    Empty dict when the config has no ``secret_ref``. Raises when the
    reference exists but the file is missing.
    """
    ref = config.get("secret_ref")
    if not ref:
        return {}
    return _secrets_module.load_secret(str(ref))


__all__ = [
    "engine_field_spec",
    "engines_dir",
    "list_available_engines",
    "list_engine_types",
    "load_engine_config",
    "locate_engine",
    "resolve_secrets",
]
