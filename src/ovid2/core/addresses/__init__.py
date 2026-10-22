"""I/O address plugins.

Every address implements :class:`BaseAddress`. Ovid2 ships:

- :class:`ovid2.drivers.disc.DiscAddress` — local/mounted CSV / Parquet / JSON.
  Migrated out of ``ovid2.core.addresses.disc`` by D3.disc.
- :class:`ovid2.core.addresses.snowflake.SnowflakeAddress` — Snowflake read/write.

Additional adapters (databricks, mssql, oracle, postgres) are stubbed but
not implemented yet.
"""

from __future__ import annotations

__all__: list[str] = []
