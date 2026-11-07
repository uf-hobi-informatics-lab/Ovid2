"""Resolve which ``secrets.d`` files an executor invocation needs — by *name* only.

Web-safe: reads address / connection / engine / LLM **config** JSON and
returns ``secret_ref`` stems. It never opens a secret file, so the web
tier may call it (``test_web_no_plaintext_secret_reads.py`` holds).

The result is the input to :func:`ovid2.config.executor_env.scoped_executor_env`
(host subprocesses) and to the docker ``secrets.d`` tmpfs shadow
(:func:`ovid2.core.docker._scoped_secret_mounts`) — one implementation
for both paths.

Resolution graph::

    address ──► connection ──► secret_ref
    engine  ──► secret_ref
            └─► args.connection ──► connection ──► secret_ref   (snowpark)
    pipeline ─► "<pipeline>"            (per-pipeline salts, optional)
    llm     ──► secret_ref

Unresolvable names are skipped (logged at debug): an unknown address
simply contributes nothing, and the executor will then fail loudly with
"secret not found" if it really needed something.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

__all__ = [
    "SecretScope",
    "refs_for_addresses",
    "refs_for_connections",
    "refs_for_engines",
    "refs_for_llms",
    "resolve_scope",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SecretScope:
    """The named resources one executor invocation may dereference.

    Attributes:
        connections: Connection names (``connections/<name>.json``).
        addresses: Address names (``configs/<name>.json``).
        engines: Engine names (``engines/<name>.json``). Bare engine
            *types* (``pyspark``) are fine — they simply have no secret.
        llms: LLM connection names (``llms/<name>.json``).
        pipelines: Pipeline names whose ``secrets.d/<pipeline>.json``
            (salts for the ``hash_id`` mapper op) the run may read.
        extra_refs: Additional explicit ``secrets.d`` stems / file names
            (e.g. the XOR seed file) — use sparingly.
    """

    connections: tuple[str, ...] = ()
    addresses: tuple[str, ...] = ()
    engines: tuple[str, ...] = ()
    pipelines: tuple[str, ...] = ()
    llms: tuple[str, ...] = ()
    extra_refs: tuple[str, ...] = field(default_factory=tuple)

    def is_empty(self) -> bool:
        """Return ``True`` when the scope names nothing."""
        return not (
            self.connections
            or self.addresses
            or self.engines
            or self.pipelines
            or self.llms
            or self.extra_refs
        )


def _connection_ref(name: str) -> str | None:
    from ovid2.config.connections import load_connection_config

    try:
        raw = load_connection_config(name)
    except (FileNotFoundError, ValueError, OSError) as exc:
        logger.debug("secret_refs: connection %r unresolvable: %s", name, exc)
        return None
    ref = raw.get("secret_ref")
    return str(ref) if ref else None


def refs_for_connections(names: Iterable[str]) -> set[str]:
    """Return the ``secret_ref`` stems of the named connections."""
    refs: set[str] = set()
    for name in names:
        if not name:
            continue
        ref = _connection_ref(str(name))
        if ref:
            refs.add(ref)
    return refs


def refs_for_addresses(names: Iterable[str]) -> set[str]:
    """Return the ``secret_ref`` stems the named addresses depend on.

    W7 addresses resolve through their ``connection``; legacy addresses
    through ``src_name`` when it names a connection.
    """
    from ovid2.config.addresses import load_address_config

    connections: list[str] = []
    for name in names:
        if not name:
            continue
        try:
            payload = load_address_config(str(name))
        except (FileNotFoundError, ValueError, OSError) as exc:
            logger.debug("secret_refs: address %r unresolvable: %s", name, exc)
            continue
        if payload.get("_shape") == "connection":
            connections.append(str(payload.get("connection") or ""))
        else:
            connections.append(str(payload.get("src_name") or ""))
    return refs_for_connections(connections)


def refs_for_engines(names: Iterable[str]) -> set[str]:
    """Return the ``secret_ref`` stems the named engines depend on.

    Includes the engine's own ``secret_ref`` and, when its ``args`` name
    a reusable ``connection`` (Snowpark), that connection's ref.
    """
    from ovid2.config.engines import load_engine_config, locate_engine

    refs: set[str] = set()
    for name in names:
        if not name or locate_engine(str(name)) is None:
            continue  # bare engine type (e.g. "pyspark") — no secret
        try:
            raw = load_engine_config(str(name))
        except (FileNotFoundError, ValueError, OSError) as exc:
            logger.debug("secret_refs: engine %r unresolvable: %s", name, exc)
            continue
        ref = raw.get("secret_ref")
        if ref:
            refs.add(str(ref))
        connection = (raw.get("args") or {}).get("connection")
        if connection:
            refs |= refs_for_connections([str(connection)])
    return refs


def refs_for_llms(names: Iterable[str]) -> set[str]:
    """Return the ``secret_ref`` stems of the named LLM connections."""
    from ovid2.config.llms import load_llm_config

    refs: set[str] = set()
    for name in names:
        if not name:
            continue
        try:
            raw = load_llm_config(str(name))
        except (FileNotFoundError, ValueError, OSError) as exc:
            logger.debug("secret_refs: llm %r unresolvable: %s", name, exc)
            continue
        ref = raw.get("secret_ref")
        if ref:
            refs.add(str(ref))
    return refs


def resolve_scope(scope: SecretScope) -> set[str]:
    """Return every ``secrets.d`` *file name* (with extension) ``scope`` grants.

    Connection / engine refs and pipeline salts are ``<stem>.json``;
    ``extra_refs`` entries are taken verbatim when they already carry an
    extension (the XOR seed is ``xor_seed.txt``), else get ``.json``.
    """
    stems: set[str] = set()
    stems |= refs_for_connections(scope.connections)
    stems |= refs_for_addresses(scope.addresses)
    stems |= refs_for_engines(scope.engines)
    stems |= refs_for_llms(scope.llms)
    stems |= {p for p in scope.pipelines if p}
    files = {f"{s}.json" for s in stems}
    for extra in scope.extra_refs:
        if extra:
            files.add(extra if "." in extra else f"{extra}.json")
    return files


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Library module; import ovid2.config.secret_refs.")
