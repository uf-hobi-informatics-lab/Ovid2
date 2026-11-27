"""Arrow → Snowflake transient-table import used by the engine bridge.

ovid2: allow-sql-fstring — every SQL statement in this module is built
from trusted internal values: the staging schema returned by
:func:`~ovid2.drivers.snowflake.staging.disc._ensure_writable_staging_schema`,
a table name sanitised with :func:`re.sub` plus a random suffix, and
double-quote-escaped Arrow column names.

:mod:`ovid2.core.engines.bridge` calls :func:`import_arrow` when a
Snowpark engine reads a driver it has no native staging strategy for.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from ovid2.drivers.snowflake.staging.disc import _ensure_writable_staging_schema

_log = logging.getLogger(__name__)


def import_arrow(native: Any, table: Any, *, name: str) -> Any:
    """Load ``table`` (``pyarrow.Table``) into a transient staging table.

    Args:
        native: The raw ``snowflake.snowpark.Session``.
        table: Arrow table to load (every column is a string at the stage
            boundary).
        name: Logical table name, used to build the staging identifier.

    Returns:
        A Snowpark ``DataFrame`` over the transient table, dropped when the
        engine session closes.
    """
    schema = _ensure_writable_staging_schema(native)
    safe = re.sub(r"[^A-Za-z0-9]+", "_", str(name)).strip("_").upper() or "SRC"
    target = f"OVID2_BRIDGE_{safe}_{uuid.uuid4().hex[:8].upper()}"
    database, _, schema_name = schema.partition(".")
    renamed = table.rename_columns([str(c).upper() for c in table.column_names])
    fqtn = f"{schema}.{target}"
    if renamed.num_rows == 0:
        # ``write_arrow`` needs at least one batch; create the empty table
        # with the frame's columns (all VARCHAR, the stage-boundary contract).
        default_col = '"_C0" VARCHAR'
        cols = ", ".join('"' + c.replace('"', '""') + '" VARCHAR' for c in renamed.column_names)
        ddl_cols = cols or default_col
        native.sql(f"CREATE OR REPLACE TRANSIENT TABLE {fqtn} ({ddl_cols})").collect()  # noqa: OVID2001 — DDL statement, not a data action
    else:
        native.write_arrow(
            renamed,
            target,
            database=database or None,
            schema=schema_name or None,
            auto_create_table=True,
            table_type="transient",
            overwrite=True,
        )
    from ovid2.core.engines.stager import register_session_cleanup

    def _drop() -> None:
        try:
            native.sql(f"DROP TABLE IF EXISTS {fqtn}").collect()  # noqa: OVID2001 — DDL cleanup of staging table
        except Exception as exc:
            _log.warning("bridge cleanup of %s failed: %s", fqtn, exc)

    register_session_cleanup(native, _drop)
    return native.table(fqtn)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Library module.")
