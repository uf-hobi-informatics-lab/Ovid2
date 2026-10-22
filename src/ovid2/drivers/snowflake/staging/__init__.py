"""Snowpark engine staging strategies for the snowflake driver package.

Each strategy under this subpackage implements the
:class:`~ovid2.core.engines.stager.SnowparkStagingStrategy` protocol
for one non-Snowflake driver type (``disc``, ``s3``, ``azure_blob``,
``databricks``, ...) and self-registers via
:func:`~ovid2.core.engines.stager.register_snowpark_strategy` at
module import time.

Phase C shipped :class:`~ovid2.drivers.snowflake.staging.disc.SnowparkDiscStrategy`.
Phase D added :class:`~ovid2.drivers.snowflake.staging.s3.SnowparkS3Strategy`
and :class:`~ovid2.drivers.snowflake.staging.azure_blob.SnowparkAzureBlobStrategy`.
Phase E added
:class:`~ovid2.drivers.snowflake.staging.databricks.SnowparkDatabricksStrategy`
(routed via an S3 bounce that both Snowflake and Databricks can
reach).
Phase F adds :class:`~ovid2.drivers.snowflake.staging.gcs.SnowparkGcsStrategy`
(Snowflake external stage against ``URL='gcs://…'``; integration-only
auth path).
"""

from __future__ import annotations

# Import each strategy module so its bottom-of-file
# ``register_snowpark_strategy()`` call runs. The snowflake driver's
# top-level ``__init__.py`` imports this subpackage, so the strategies
# self-register whenever the driver is loaded (which happens
# unconditionally during CLI/web startup).
from ovid2.drivers.snowflake.staging import azure_blob as _azure_blob  # noqa: F401
from ovid2.drivers.snowflake.staging import databricks as _databricks  # noqa: F401
from ovid2.drivers.snowflake.staging import disc as _disc  # noqa: F401
from ovid2.drivers.snowflake.staging import gcs as _gcs  # noqa: F401
from ovid2.drivers.snowflake.staging import s3 as _s3  # noqa: F401

__all__: list[str] = []
