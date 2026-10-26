"""Contract tests for Snowpark staging strategies.

Ensures the Phase-C+ contract that each driver-type strategy under
``ovid2.drivers.snowflake.staging.*`` self-registers on the
:class:`~ovid2.core.engines.stager.SnowparkEngineStager` dispatch
map at driver-package import time — without dragging in
``snowflake.snowpark`` at module scope.

Phase C ships :class:`~ovid2.drivers.snowflake.staging.disc.SnowparkDiscStrategy`.
Phase D will add ``s3`` and ``azure_blob``.
"""

from __future__ import annotations

import sys

import pytest


@pytest.mark.contract
def test_snowpark_disc_strategy_self_registers_on_driver_import() -> None:
    """Importing :mod:`ovid2.drivers.snowflake` must register the disc strategy.

    The snowflake driver's ``__init__.py`` imports the staging
    subpackage, which imports :mod:`ovid2.drivers.snowflake.staging.disc`,
    which calls
    :func:`~ovid2.core.engines.stager.register_snowpark_strategy`
    at module bottom. This is the mechanism that keeps the compat
    check's ``supports('disc')`` reply consistent with the actual
    data-plane capability.
    """
    # Force a fresh import chain.
    for mod in list(sys.modules):
        if mod == "ovid2.drivers.snowflake" or mod.startswith("ovid2.drivers.snowflake."):
            del sys.modules[mod]
        if mod == "ovid2.core.engines.stager":
            del sys.modules[mod]

    import ovid2.drivers.snowflake  # noqa: F401
    from ovid2.core.engines.stager import SnowparkEngineStager

    strategies = SnowparkEngineStager._STRATEGIES
    assert "disc" in strategies, (
        "SnowparkDiscStrategy failed to self-register. Check that "
        "src/ovid2/drivers/snowflake/staging/__init__.py imports the "
        "disc module and that disc.py calls register_snowpark_strategy() "
        "at module bottom."
    )
    assert strategies["disc"].driver_type == "disc"


@pytest.mark.contract
def test_snowpark_staging_import_stays_web_safe() -> None:
    """Importing every strategy module must NOT drag pyspark in.

    Phase-C staging strategies talk to Snowpark, not Spark. If any
    strategy module accidentally imports pyspark (typically via a
    stray ``from ovid2.drivers.<x>.spark import ...`` at module
    scope), the compat check's web-tier callers
    (:func:`ovid2.web.services.jobs_svc._validate_submit`,
    :func:`ovid2.config.engines.engine_family`) would drag pyspark
    into the web process. That breaks
    ``test_web_no_pipeline_imports.py``. Catch it here first.
    """
    # Fresh import chain.
    for mod in list(sys.modules):
        if mod.startswith("ovid2.drivers.snowflake.staging"):
            del sys.modules[mod]
        if mod.startswith("pyspark"):
            del sys.modules[mod]

    import ovid2.drivers.snowflake.staging  # noqa: F401

    pyspark_loaded = [m for m in sys.modules if m.startswith("pyspark")]
    assert not pyspark_loaded, (
        "ovid2.drivers.snowflake.staging import pulled in pyspark modules: "
        f"{pyspark_loaded}. Strategy modules must NOT import pyspark at "
        "module scope — Snowpark and Spark are mutually exclusive. Move "
        "any pyspark import into a function body (as SnowparkDiscStrategy "
        "does for detect_delimiter / resolve_read_path)."
    )
