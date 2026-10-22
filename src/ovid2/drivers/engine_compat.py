"""Engine/driver compatibility check.

Consulted at plan time (before a Spark cluster or Snowpark session is
started) to refuse an incompatible engine/driver pairing early. As of
Phase A+B of the engine/storage-orthogonality plan, the check is
**stager-based**: it asks the engine's
:class:`~ovid2.core.engines.stager.EngineStager` whether it has a
strategy for the driver, rather than asking the driver whether it
supports the engine's family.

Historical failure mode this closes
-----------------------------------

::

    Operator: ovid2 tools copy --from CHAR_DEMO --to S3-Testing \\
              --pipeline demo --batch_id 20260610 --tables HASH_TOKEN \\
              --engine snowpark

    Container:  from snowflake.snowpark import Session
    Container:  ModuleNotFoundError: No module named 'snowflake.snowpark'

The immediate crash surfaces the missing wheel; the underlying error
is semantic — Snowpark DataFrames live inside Snowflake, so a Snowpark
session cannot write to ``s3a://`` (or read from a disc CSV, or push to
Azure Blob) without staging the data through a Snowflake stage first.
Phase A+B keeps the rejection surface byte-identical to the pre-plan
world; Phase C/D will add the staging strategies that make some of
these pairings work.

Why "plan time" and not "get_session()"
---------------------------------------

By the time :meth:`SnowparkEngine.get_session` runs, the host-side
driver has already started a 25-worker Spark cluster (via
:func:`ovid2.core.docker.submit_with_cluster`), the compose file is on
disk, and every worker container is up. Deferring the check to session
build means:

* Every failed submission pays a full cluster-start cost (~15s minimum).
* The traceback the operator sees is a raw ``ModuleNotFoundError`` or
  a Snowpark-native ``SnowparkSQLException`` — neither of which
  explains *why* the pairing was wrong.

The check runs on the host side in :func:`ovid2.tools.copy.main`
before ``_run_host_mode`` reaches ``submit_to_docker`` /
``submit_with_cluster``, and again inside the container in
:func:`~ovid2.tools.copy._run_container_mode` before
``engine.get_session()``. Belt + suspenders: the host-side call saves
the cluster-start cost; the container-side call catches the edge case
where an operator calls ``ovid2 tools copy`` directly from inside a
running container.

Sources consulted
-----------------

* :meth:`EngineStager.supports(driver_type)` — declared on the
  stager, per engine family.
* :attr:`BaseEngine.family` — declared on the engine class. Values
  are :class:`~ovid2.core.engines.protocols.EngineFamily` enum
  members.
* :func:`ovid2.config.engines.engine_family` (web-safe) — used by
  the type-name variant to avoid loading a real
  :class:`BaseEngine`.

The check succeeds iff ``stager.supports(driver.type_name)`` returns
``True`` for every address involved. Missing drivers (address whose
type is not registered) are treated as *unknown-compatible* — the
check emits a debug log line and lets the run proceed, so a legacy
shape that predates the driver registry never blocks a submission.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ovid2.core.engines.base import BaseEngine
    from ovid2.core.engines.stager import EngineStager
    from ovid2.drivers.base import Driver

logger = logging.getLogger(__name__)


def _driver_class_for_address(address_name: str) -> type[Driver] | None:
    """Return the driver class for ``address_name``, or ``None``.

    Mirrors the resolution in
    :func:`ovid2.drivers.type_stamping.declared_cdm_for_address` — same
    W7-vs-legacy branching, same silent ``None`` on any resolution
    failure. Kept as a private helper here (rather than exported from
    ``type_stamping``) because the return type differs (a class object
    vs a CDM name string) and duplicating a five-line function is
    cleaner than exporting an internal.

    Args:
        address_name: Filename stem of a config under ``configs/``.

    Returns:
        The concrete ``Driver`` subclass registered for the address's
        type, or ``None`` when the address cannot be resolved (missing
        file, malformed JSON, unregistered type).
    """
    from ovid2.config.addresses import load_address_config
    from ovid2.config.connections import load_connection_config
    from ovid2.drivers.registry import DRIVERS, load_class

    try:
        payload = load_address_config(address_name)
    except (FileNotFoundError, ValueError):
        return None

    shape = payload.get("_shape")
    if shape == "connection":
        connection_name = str(payload.get("connection") or "")
        if not connection_name:
            return None
        try:
            conn_raw = load_connection_config(connection_name)
        except (FileNotFoundError, ValueError):
            return None
        type_name = str(conn_raw.get("type") or "")
    else:
        type_name = str(payload.get("src_name") or "")

    if type_name not in DRIVERS:
        return None
    try:
        return load_class(type_name)
    except (KeyError, ImportError, AttributeError):
        return None


def assert_engine_supports_drivers(
    engine: BaseEngine,
    address_names: Iterable[str],
) -> None:
    """Refuse the run when the engine's stager cannot reach every driver.

    Args:
        engine: A loaded :class:`BaseEngine` instance. The check reads
            :attr:`BaseEngine.family` (an
            :class:`~ovid2.core.engines.protocols.EngineFamily`) and
            :attr:`BaseEngine.type_name` (for the error message), and
            calls :meth:`BaseEngine.get_stager` to obtain the
            :class:`EngineStager` whose
            :meth:`~EngineStager.supports` classmethod drives the
            actual check.
        address_names: Address names involved in the run — for
            ``ovid2 tools copy`` this is the ``--from`` and ``--to``
            pair; for ``ovid2 tools move`` the same; for a hypothetical
            multi-address extension, every touched endpoint.

    Raises:
        RuntimeError: When any address's driver is not supported by
            the engine's stager. The error message names the
            incompatible pair, lists the driver types the stager
            *does* support, and suggests ``--engine pyspark`` as the
            reliable cross-peripheral fallback.

    Addresses whose driver cannot be resolved (unregistered type,
    missing config file, malformed JSON) are skipped with a debug
    log line — the run proceeds, and any downstream failure gets to
    speak for itself.
    """
    from ovid2.core.engines.protocols import EngineFamily

    engine_family_value = (
        engine.family.value if isinstance(engine.family, EngineFamily) else str(engine.family)
    )
    engine_type = getattr(engine, "type_name", type(engine).__name__)
    stager = engine.get_stager()
    _assert_stager_supports_drivers(
        stager_cls=type(stager),
        engine_family=engine_family_value,
        engine_type=engine_type,
        address_names=address_names,
    )


def assert_engine_type_supports_drivers(
    engine_type: str,
    address_names: Iterable[str],
) -> None:
    """Web-safe variant that consults the type name only.

    Same semantics as :func:`assert_engine_supports_drivers` — refuses
    a run whose engine's stager has no strategy for one of the
    addresses — but takes the *engine type name string* (e.g.
    ``"snowpark"``) rather than a loaded :class:`BaseEngine` instance.
    This lets pyspark-free callers (the web tier, in particular
    :mod:`ovid2.web.services.jobs_svc`) run the check without
    importing :mod:`ovid2.core.engines` or :mod:`ovid2.core.loaders`.

    The engine family is resolved via
    :func:`ovid2.config.engines.engine_family`, then the family →
    stager map is consulted via
    :func:`ovid2.core.engines.stager.stager_class_for_engine_type`.
    When the type name is unknown the check is skipped (a debug log
    line is emitted); the caller — typically the worker or CLI — is
    expected to reject the run for a different reason (``load_engine``
    will raise with a concrete message).

    Args:
        engine_type: Engine type name. Values known today are
            ``"pyspark"``, ``"databricks_spark"``, ``"snowpark"``.
            Config-file *names* (e.g. ``"pyspark-prod"``) are also
            accepted and treated as unknown → the check skips.
        address_names: Same as :func:`assert_engine_supports_drivers`.

    Raises:
        RuntimeError: When any address's driver is not supported by
            the engine's stager.
    """
    from ovid2.config.engines import engine_family
    from ovid2.core.engines.stager import stager_class_for_engine_type

    stager_cls = stager_class_for_engine_type(engine_type)
    if stager_cls is None:
        logger.debug(
            "engine_compat: engine type %r not in the web-safe family map; "
            "skipping check (host-side gate will still fire).",
            engine_type,
        )
        return
    family = engine_family(engine_type)
    _assert_stager_supports_drivers(
        stager_cls=stager_cls,
        engine_family=str(family or ""),
        engine_type=engine_type,
        address_names=address_names,
    )


def _assert_stager_supports_drivers(
    *,
    stager_cls: type[EngineStager],
    engine_family: str,
    engine_type: str,
    address_names: Iterable[str],
) -> None:
    """Shared body for the two public entry points.

    Both :func:`assert_engine_supports_drivers` (instance form, used by
    :mod:`ovid2.tools.copy` and :mod:`ovid2.tools.move`) and
    :func:`assert_engine_type_supports_drivers` (type-name form, used
    by the web tier) resolve to a stager *class* and delegate here.
    The classmethod pair
    :meth:`EngineStager.supports` /
    :meth:`EngineStager.supported_driver_types` is enough to make the
    decision — no engine session is built, no strategy is instantiated.

    The error message names the incompatible pair, the stager's
    supported set, and the escape hatch (``--engine pyspark`` — the
    only engine whose stager accepts every registered driver today).
    """
    for address_name in address_names:
        driver_cls = _driver_class_for_address(address_name)
        if driver_cls is None:
            logger.debug(
                "engine_compat: address %r has no resolvable driver; "
                "skipping stager compatibility check.",
                address_name,
            )
            continue
        driver_type = getattr(driver_cls, "type_name", driver_cls.__name__)
        try:
            supported_ok = stager_cls.supports(driver_type)
        except Exception:
            logger.debug(
                "engine_compat: stager %r raised on supports(%r); skipping check for address %r.",
                stager_cls.__name__,
                driver_type,
                address_name,
            )
            continue
        if supported_ok:
            continue
        try:
            supported = sorted(stager_cls.supported_driver_types())
        except Exception:
            supported = []
        raise RuntimeError(
            f"Engine '{engine_type}' (family '{engine_family}') is not "
            f"compatible with address '{address_name}' (driver "
            f"'{driver_type}'). The '{engine_family}' engine's stager "
            f"supports driver types {supported}. Snowpark DataFrames "
            f"live inside Snowflake and cannot read/write non-Snowflake "
            f"peripherals directly; use '--engine pyspark' for "
            f"cross-peripheral copies (the Bitnami base image ships "
            f"the JDBC + hadoop-aws + ABFS drivers)."
        )


__all__ = [
    "assert_engine_supports_drivers",
    "assert_engine_type_supports_drivers",
]
