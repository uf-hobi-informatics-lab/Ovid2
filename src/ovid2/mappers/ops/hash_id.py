"""``hash_id`` — deterministically hash an identifier column.

Replaces the legacy per-family XOR-seed encryption with a Spark-native
SHA-2 based hash so the pipeline stays lazy end-to-end. The seed comes
from :attr:`MapOpContext.secrets` which the engine loads from
``$XDG_CONFIG_HOME/ovid2/secrets.d/<pipeline>.json`` — never inline in
the mapper spec.

Params:
    column: Column to hash.
    salt_key: Key in ``ctx.secrets`` whose value is the per-pipeline
        salt string. Defaults to ``'id_salt'``.
    algorithm: Hash algorithm. Currently only ``'sha256'`` is supported.
    output_length: Optional integer — truncate the hex digest to this
        many characters. Defaults to the CDM's harmonised width for the
        column's identifier role when available; otherwise the full
        digest is retained.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ovid2.mappers.base import BaseMapOp, MapOpContext

if TYPE_CHECKING:
    from pyspark.sql import DataFrame

logger = logging.getLogger(__name__)


class HashIdOp(BaseMapOp):
    """Deterministically hash an identifier column using a per-pipeline salt."""

    @property
    def name(self) -> str:
        """Return the op identifier."""
        return "hash_id"

    def apply(
        self,
        df: "DataFrame",
        params: dict[str, Any],
        ctx: MapOpContext,
    ) -> "DataFrame":
        """Return ``df`` with the named column replaced by its hash."""
        from pyspark.sql.functions import col as sf_col
        from pyspark.sql.functions import concat_ws, lit, sha2, substring

        column = str(params["column"])
        salt_key = str(params.get("salt_key", "id_salt"))
        algorithm = str(params.get("algorithm", "sha256")).lower()
        if algorithm != "sha256":
            raise ValueError(
                f"hash_id: unsupported algorithm '{algorithm}'; only 'sha256' is available."
            )

        salt = ""
        if ctx.secrets is not None:
            salt = str(ctx.secrets.get(salt_key, "") or "")
        if not salt:
            logger.warning(
                "hash_id: no salt found for pipeline '%s' at secrets key '%s'; hashing without salt",
                ctx.pipeline,
                salt_key,
            )

        # Look up the desired output length: explicit param wins; otherwise
        # find the CDM id role whose column matches and use its width.
        output_length = params.get("output_length")
        if output_length is None:
            for id_col in ctx.cdm.id_columns.values():
                if id_col.column == column:
                    output_length = id_col.width
                    break

        hashed = sha2(concat_ws("|", lit(salt), sf_col(column).cast("string")), 256)
        if output_length is not None:
            hashed = substring(hashed, 1, int(output_length))
        return df.withColumn(column, hashed)
