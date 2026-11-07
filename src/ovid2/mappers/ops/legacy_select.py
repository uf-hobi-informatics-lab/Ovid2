"""``legacy_select`` — reproduce a legacy-Ovid mapper's ``select(...)`` verbatim.

Every legacy ``mapping_scripts/<family>_mappers/<table>_mapper.py`` builds
its output as **one** ``unmapped.select(...)`` whose items are drawn from a
small, closed grammar (measured over all 43 legacy mappers), optionally
followed by ``append_additional_fields``. This op takes that projection
list declaratively, so each legacy mapper becomes a YAML spec with no
Python.

Params:
    columns: Ordered list. Each item is either a bare column name
        (``unmapped['X']`` — output name ``X``) or a one-key mapping
        ``{OUT: <expr>}`` where ``<expr>`` is one of:

        ``{col: X}``                       ``unmapped['X']`` / ``col('X')``
        ``{col: X, cast: T}``              ``col('X').cast(T)``
        ``{encrypt: X}``                   ``cf.encrypt_id_udf(unmapped['X'])``
        ``{encrypt: X, suffix: S}``        ``cf.encrypt_id_udf(concat(X, lit(S)))``
        ``{dict: D, col: X}``              ``coalesce(D[upper(col(X))], col(X))``
        ``{dict: D, col: X, upper: false}`` ``coalesce(D[col(X)], col(X))``
        ``{dict: D, col: X, fallback: none}``  ``D[upper(col(X))]`` (no pass-through)
        ``{dict: D, col: X, fallback: {lit: V}}``  ``coalesce(D[upper(col(X))], lit(V))``
        ``{coalesce: [<expr>, ...]}``      ``coalesce(...)`` of sub-expressions,
                                           where a sub-expression may also be
                                           ``{nonblank: X}`` = ``when(col(X) != '', col(X))``
                                           or ``{dict_only: D, col: X}`` = ``D[upper(col(X))]``
        ``{lit: V}``                       ``lit(V)``
        ``{updated: true}``                ``cf.get_current_time_udf()`` (America/New_York)
        ``{source: true}``                 ``lit(partner.upper())``
        ``{udf: NAME, args: [X, ...]}``    a named function from
                                           :data:`ovid2.legacy.mapper_udfs.MAPPER_UDFS`

    pre_join: Optional ``geocoding`` — first reproduce the legacy address
        mappers' ``joined_geocoding`` frame (left joins of the
        ``FULL_GEOCODING`` reference on ``ADDRESS_ZIP9`` and, for ZIP5-only
        rows, on ``ADDRESS_ZIP5``); projected columns then resolve against it.
    append_additional_fields: Optional ``{join_field: X}`` — reproduces
        legacy ``cf.append_additional_fields``: when the input carries more
        than ``len(columns) - 3`` columns, every input column *not* already
        projected is appended (inner join on ``X``, which legacy carried as
        the hidden ``JOIN_FIELD``). Otherwise nothing is appended. Legacy
        re-read the deduplicated file with ``inferSchema="true"`` for the
        appended columns, so they carry inferred-type text
        (:func:`ovid2.legacy.csv_roundtrip.legacy_infer_schema_view`).

Dictionaries resolve against the pipeline's ``dictionaries`` module; a
missing dict raises (legacy raised ``AttributeError`` and the table failed).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from ovid2.mappers.base import BaseMapOp, MapOpContext

if TYPE_CHECKING:
    from pyspark.sql import Column, DataFrame

_JOIN_FIELD = "__legacy_join_field"
_JOIN_FIELD_NEW = "__legacy_join_field_new"


class LegacySelectOp(BaseMapOp):
    """Project ``df`` exactly like a legacy mapper's ``select(...)``."""

    @property
    def name(self) -> str:
        """Return the op identifier."""
        return "legacy_select"

    # ------------------------------------------------------------------ #

    def apply(self, df: DataFrame, params: dict[str, Any], ctx: MapOpContext) -> DataFrame:
        """Return the projected (and optionally appended) frame."""
        from ovid2.core.engines.dsl import col

        items = list(params.get("columns") or [])
        if not items:
            raise ValueError(f"legacy_select on '{ctx.table}': 'columns' must be non-empty.")
        source = df
        if params.get("pre_join") == "geocoding":
            source = self._join_geocoding(df, ctx)
        maps: dict[str, Column] = {}
        projected: list[Column] = []
        out_names: list[str] = []
        for item in items:
            out, expr = self._item(item)
            projected.append(self._expr(expr, source, ctx, maps).alias(out))
            out_names.append(out)

        append = params.get("append_additional_fields")
        if not append:
            return source.select(*projected)

        join_field = str(append["join_field"])
        # Legacy compares the *deduplicated file* column count against the
        # mapped select (which included the hidden JOIN_FIELD): append only
        # when ``len(input) + 3 > len(select incl. JOIN_FIELD)``.
        if not len(df.columns) + 3 > len(out_names) + 1:
            return source.select(*projected)

        from ovid2.legacy.csv_roundtrip import legacy_infer_schema_view, source_text

        mapped = source.select(*projected, col(f"`{join_field}`").alias(_JOIN_FIELD))
        common = set(out_names) & set(df.columns)
        extra = (
            legacy_infer_schema_view(source_text(df), ctx.spark)
            .drop(*[c for c in common if c != join_field])
            .withColumnRenamed(join_field, _JOIN_FIELD_NEW)
        )
        joined = mapped.join(extra, mapped[_JOIN_FIELD] == extra[_JOIN_FIELD_NEW])
        return joined.drop(_JOIN_FIELD, _JOIN_FIELD_NEW)

    @staticmethod
    def _join_geocoding(df: DataFrame, ctx: MapOpContext) -> DataFrame:
        """Legacy ``lds/private_address_history_mapper`` geocoding join (verbatim)."""
        from ovid2.config.settings import COMMON_LISTS_DIR
        from ovid2.core.engines.dsl import col, concat, trim
        from ovid2.legacy import reference

        path = reference.get_geocoding_path(str(COMMON_LISTS_DIR).rstrip("/") + "/")
        geocoding = ctx.spark.read.load(
            path, format="csv", sep=",", inferSchema="false", header="true", quote='"'
        )
        geocoding = geocoding.withColumn(
            "FULL_GEOCODE_COUNTY", concat(col("GEOCODE_STATE"), col("GEOCODE_COUNTY"))
        )
        geocoding_zip5 = (
            geocoding.withColumn("ZIP9", col("ZIP9").cast("string"))
            .filter(col("ZIP9").isNull() | (trim(col("ZIP9")) == ""))
            .select(
                col("ZIP5").cast("string").alias("NEW_ZIP5"),
                col("GEOCODE_STATE").alias("STATE_FIPS_ZIP5"),
                col("FULL_GEOCODE_COUNTY").alias("COUNTY_FIPS_ZIP5"),
            )
        )
        return df.join(geocoding, geocoding["ZIP9"] == df["ADDRESS_ZIP9"], how="left").join(
            geocoding_zip5, geocoding_zip5["NEW_ZIP5"] == df["ADDRESS_ZIP5"], how="left"
        )

    # ------------------------------------------------------------------ #

    @staticmethod
    def _item(item: Any) -> tuple[str, dict[str, Any]]:
        """Normalise one ``columns`` entry to ``(output_name, expr_dict)``."""
        if isinstance(item, str):
            return item, {"col": item}
        if isinstance(item, dict) and len(item) == 1:
            ((out, expr),) = item.items()
            if isinstance(expr, str):
                expr = {"col": expr}
            if not isinstance(expr, dict):
                raise ValueError(f"legacy_select: bad expression for '{out}': {expr!r}")
            return str(out), expr
        raise ValueError(
            f"legacy_select: each column must be a name or a one-key mapping: {item!r}"
        )

    def _map_literal(self, dict_name: str, ctx: MapOpContext, cache: dict[str, Column]) -> Column:
        """Return ``create_map`` for the named pipeline dictionary (cached)."""
        from ovid2.core.engines.dsl import create_map, lit

        if dict_name in cache:
            return cache[dict_name]
        if dict_name.startswith("@geo:"):
            # Legacy ``create_map(cf.get_xxx().items())`` geocoding lookups.
            from ovid2.config.settings import COMMON_LISTS_DIR
            from ovid2.legacy import reference

            fn = getattr(reference, dict_name[len("@geo:") :])
            mapping = fn(str(COMMON_LISTS_DIR).rstrip("/") + "/")
        else:
            mapping = getattr(ctx.pipeline_dictionaries, dict_name, None)
        if not isinstance(mapping, dict):
            raise ValueError(
                f"legacy_select on '{ctx.table}': dictionary '{dict_name}' not found in "
                f"ovid2_pipelines.{ctx.pipeline}.dictionaries."
            )
        pairs: list[Any] = []
        for key, value in mapping.items():
            pairs.extend([lit(key), lit(value)])
        expr = create_map(*pairs) if pairs else create_map()
        cache[dict_name] = expr
        return cast("Column", expr)

    def _expr(
        self, e: dict[str, Any], df: DataFrame, ctx: MapOpContext, cache: dict[str, Column]
    ) -> Column:
        """Translate one expression dict into a Spark column."""
        return cast("Column", self._expr_any(e, df, ctx, cache))

    def _expr_any(
        self, e: dict[str, Any], df: DataFrame, ctx: MapOpContext, cache: dict[str, Column]
    ) -> Any:
        """Untyped body of :meth:`_expr` (the DSL resolves per engine)."""
        from ovid2.core.engines.dsl import coalesce, col, concat, lit, upper, when

        if "encrypt" in e:
            from ovid2.legacy.udfs import legacy_udfs

            target = col(f"`{e['encrypt']}`")
            if "suffix" in e:
                target = concat(target, lit(str(e["suffix"])))
            return legacy_udfs(ctx.pipeline).encrypt_id(target)
        if e.get("updated"):
            from ovid2.legacy.udfs import legacy_udfs

            return legacy_udfs(ctx.pipeline).current_time()
        if e.get("source"):
            return lit(ctx.pipeline.upper())
        if "lit" in e:
            return lit(e["lit"])
        if "nonblank" in e:
            c = col(f"`{e['nonblank']}`")
            return when(c != lit(""), c)
        if "dict_only" in e:
            m = self._map_literal(str(e["dict_only"]), ctx, cache)
            c = col(f"`{e['col']}`")
            return m[upper(c) if e.get("upper", True) else c]
        if "dict" in e:
            m = self._map_literal(str(e["dict"]), ctx, cache)
            c = col(f"`{e['col']}`")
            looked_up = m[upper(c) if e.get("upper", True) else c]
            fallback = e.get("fallback", "col")
            if fallback == "none":
                return looked_up
            if isinstance(fallback, dict):
                return coalesce(looked_up, self._expr(fallback, df, ctx, cache))
            return coalesce(looked_up, c)
        if "coalesce" in e:
            return coalesce(*[self._expr(sub, df, ctx, cache) for sub in e["coalesce"]])
        if "udf" in e:
            from ovid2.legacy.mapper_udfs import mapper_udf

            fn = mapper_udf(str(e["udf"]), ctx)
            return fn(*[col(f"`{a}`") for a in e.get("args", [])])
        if "col" in e:
            c = col(f"`{e['col']}`")
            return c.cast(str(e["cast"])) if "cast" in e else c
        raise ValueError(f"legacy_select on '{ctx.table}': unsupported expression {e!r}")
