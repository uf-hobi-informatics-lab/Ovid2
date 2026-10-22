"""Driver protocol — the ``struct driver_ops`` of Ovid2.

Ovid2 talks to storage endpoints (S3, Azure Blob, Snowflake, disc,
Postgres, MSSQL, Databricks, GCS, and any new peripheral you drop in
tomorrow) through a single class per peripheral: the **driver**. The
MIPS-peripheral analogy is intentional:

- One driver **owns** the peripheral end-to-end — control plane
  (credentials, probe, container requirements) *and* data plane
  (read / write against an engine session).
- The pipeline treats every peripheral uniformly through this class;
  it never has to know whether the bytes live in an S3 object, a
  Snowflake table, or a local disc directory.
- Adding a new peripheral = one folder under ``ovid2.drivers.<type>``
  and one entry in :mod:`ovid2.drivers.registry`. No other file in
  the tree needs to know.

Split of concerns (per driver folder):

- ``metadata.py`` — data-only class methods. Imported by the web
  tier, MCP server, and CLI without any Spark / SDK / boto / azure
  imports. This is what the *catalogue* views (connection fields,
  location fields, container requirements) consume.
- ``driver.py`` — the concrete :class:`Driver` subclass. Imports its
  own ``metadata`` module. May import SDKs (boto, azure-storage-blob,
  snowflake-connector-python) *lazily inside methods* so the web
  tier can still walk the registry without pulling every heavy SDK.
- ``probe.py`` (optional) — the ``probe_auth`` / ``probe_read`` /
  ``probe_write`` implementations. May import SDKs lazily.
- ``spark.py`` (optional) — the ``read()`` / ``write()`` bodies.
  This is the *only* file per driver that may ``import pyspark``.
  The contract test ``test_no_pyspark_outside_engine.py`` recognises
  the pattern ``ovid2/drivers/*/spark.py`` so new drivers pick up
  the allow-list automatically.

Backwards-compatibility promise
-------------------------------

For the entire Driver refactor (phases D1–D5) the legacy
:mod:`ovid2.connections` and :mod:`ovid2.core.addresses` packages
continue to work verbatim. The :mod:`ovid2.drivers.legacy_adapter`
module (added in D2) wraps every existing ``BaseConnection`` /
``BaseAddress`` pair as a synthetic :class:`Driver` so callers can
migrate to the new registry immediately. Native driver ports
(D3.disc, D3.s3, …) replace those adapter entries one at a time.
"""

from __future__ import annotations

from ovid2.drivers.base import (
    ConnectionFieldSpec,
    Driver,
    FieldSpec,
    LocationField,
    ProbeResult,
    StorageEntry,
)
from ovid2.drivers.container import (
    ContainerRequirement,
    EnvOverlay,
    ExtraMount,
    JavaPackage,
)

__all__ = [
    "ConnectionFieldSpec",
    "ContainerRequirement",
    "Driver",
    "EnvOverlay",
    "ExtraMount",
    "FieldSpec",
    "JavaPackage",
    "LocationField",
    "ProbeResult",
    "StorageEntry",
]
