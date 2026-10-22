"""Standard probe harness for :class:`Driver` instances.

Every driver implements at least :meth:`Driver.probe_auth`. Read and
write probes are optional and raise :class:`NotImplementedError` in
the base class when a driver hasn't ported them. The harness treats
that as *capability absent* — it emits a ``code='not_implemented'``
result but does not fail the overall run.

Two entry points:

- :func:`run_all_probes` — walks auth, read, write in order and
  returns the full list. Callers that want to display every
  capability's status (a rich admin panel) use this.
- :func:`run_test` — runs only ``probe_auth`` and downgrades the
  result to :class:`~ovid2.connections.base.ConnectionTestResult`
  for the *Test connection* UI button. This is the drop-in
  replacement for the per-driver ``test()`` methods that lived on
  :class:`~ovid2.connections.base.BaseConnection`.

Callers must not construct :class:`ProbeResult` directly for the
"probe not implemented" case; use :func:`_not_implemented` so the
message stays uniform across drivers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from ovid2.drivers.base import ProbeResult

if TYPE_CHECKING:
    from ovid2.connections.base import ConnectionTestResult
    from ovid2.drivers.base import Driver

logger = logging.getLogger(__name__)


def _not_implemented(driver: Driver, capability: str) -> ProbeResult:
    """Return a uniform ``not_implemented`` result."""
    return ProbeResult(
        ok=False,
        code="not_implemented",
        message=f"Driver '{driver.type_name}' does not implement probe_{capability}.",
        capability=capability,
    )


def _run_one(driver: Driver, capability: str) -> ProbeResult:
    """Run a single probe safely, catching driver-side exceptions.

    Any exception raised by the driver's probe method becomes a
    ``code='error'`` result — the harness never propagates. This
    keeps the caller (usually the web tier) uniform: it never sees
    an unhandled traceback from a driver bug.
    """
    method = getattr(driver, f"probe_{capability}", None)
    if method is None:
        return _not_implemented(driver, capability)
    try:
        result = method()
    except NotImplementedError:
        return _not_implemented(driver, capability)
    except Exception as exc:
        logger.exception(
            "Driver '%s' probe_%s raised",
            driver.type_name,
            capability,
        )
        return ProbeResult(
            ok=False,
            code="error",
            message=f"probe_{capability} raised: {exc.__class__.__name__}: {exc}",
            capability=capability,
        )

    if not isinstance(result, ProbeResult):
        # Driver returned the wrong shape — surface it plainly rather
        # than silently swallowing.
        return ProbeResult(
            ok=False,
            code="error",
            message=(
                f"Driver '{driver.type_name}' probe_{capability} returned "
                f"{type(result).__name__} instead of ProbeResult."
            ),
            capability=capability,
        )

    # Ensure capability is stamped correctly even if the driver forgot.
    if result.capability != capability:
        return ProbeResult(
            ok=result.ok,
            code=result.code,
            message=result.message,
            capability=capability,
            detail=result.detail,
        )
    return result


def run_all_probes(driver: Driver) -> list[ProbeResult]:
    """Run every capability probe, in order: auth → read → write.

    Continues past a failed probe (a bad auth doesn't skip read —
    the caller decides). Read and write are skipped only when the
    driver explicitly raises :class:`NotImplementedError`.

    Returns:
        The full list of results, one per capability.
    """
    return [
        _run_one(driver, "auth"),
        _run_one(driver, "read"),
        _run_one(driver, "write"),
    ]


def run_test(driver: Driver) -> ConnectionTestResult:
    """Run the auth probe and downgrade to the legacy shape.

    Drop-in replacement for
    :meth:`ovid2.connections.base.BaseConnection.test`. Used by the
    *Test connection* button in the web control panel.
    """
    result = _run_one(driver, "auth")
    return cast("ConnectionTestResult", result.to_legacy_test_result())


__all__ = ["run_all_probes", "run_test"]
