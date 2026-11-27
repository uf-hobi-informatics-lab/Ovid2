"""PySpark-shaped DataFrame over a lazy DuckDB relation (the DuckDB engine).

Shared Ovid2 code (formatters, mapper ops, fixers, the deduplicator)
is written against the PySpark ``DataFrame`` / ``Column`` surface.
:class:`DuckFrame` provides that surface over a
``duckdb.DuckDBPyRelation`` so the code runs unchanged:

* **Lazy.** Every method returns a new ``DuckFrame`` over a new lazy
  relation; nothing executes until a writer (or an explicit
  :meth:`collect`) pulls rows.
* **Qualifiers.** Spark resolves ``df["x"]`` / ``col("alias.x")`` by
  attribute identity. Each frame carries a *scope*
  (:class:`~._duck_column.ScopeEntry` per visible relation) and a
  *lineage* (every alias it descends from) so a qualifier written
  against an ancestor still resolves after joins / filters, and degrades
  to an unqualified reference after a projection — mirroring Spark.
* **Duplicate column names** after a join are legal (as in Spark);
  :meth:`select` / :meth:`toDF` normalise them.
* **Implicit casts.** Column kinds are tracked so comparisons and
  arithmetic insert the casts Spark would (``string > 0`` etc.).

ovid2: allow-sql-fstring — every interpolated value is either an
identifier quoted by :func:`~._duck_column.ident`, a literal rendered by
:func:`~._duck_column.literal_sql`, or SQL produced by
:mod:`._duck_expr` from pipeline-authored expressions. No operator data
reaches these strings.
"""

from __future__ import annotations

import contextlib
import itertools
import re
from collections.abc import Iterable, Sequence
from typing import Any

from ovid2.core.engines._duck_column import (
    DuckColumn,
    RenderCtx,
    ScopeEntry,
    columns_from_args,
    ident,
    to_column,
)
from ovid2.core.engines._duck_expr import transpile, transpile_typed

__all__ = ["DuckFrame", "DuckGroupedData", "DuckRow", "DuckWindow", "DuckWindowSpec"]

_alias_counter = itertools.count(1)  # noqa: OVID2001 — itertools, not a DataFrame action


def _fresh_alias(prefix: str = "__ov") -> str:
    return f"{prefix}{next(_alias_counter)}"


class DuckRow(tuple):  # type: ignore[type-arg]
    """Tuple with attribute / key access (PySpark ``Row`` stand-in)."""

    __fields__: tuple[str, ...] = ()

    def __new__(cls, values: Sequence[Any], fields: Sequence[str]) -> DuckRow:
        row = super().__new__(cls, values)
        row.__fields__ = tuple(fields)
        return row

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            return tuple.__getitem__(self, self.__fields__.index(key))
        return tuple.__getitem__(self, key)

    def __getattr__(self, name: str) -> Any:
        try:
            return tuple.__getitem__(self, self.__fields__.index(name))
        except ValueError as exc:
            raise AttributeError(name) from exc

    def asDict(self) -> dict[str, Any]:  # noqa: N802 — PySpark API
        """Return ``{field: value}``."""
        return dict(zip(self.__fields__, self, strict=True))


class _SqlStringResolver:
    """Rewrite ``qualifier.column`` refs in a Spark SQL string to the scope."""

    _QUAL = re.compile(r"(?<![\w.`\"'])([A-Za-z_][A-Za-z0-9_]*)\.(`[^`]+`|[A-Za-z_][A-Za-z0-9_]*)")

    def __init__(self, ctx: RenderCtx) -> None:
        self._ctx = ctx

    def rewrite(self, sql: str) -> str:
        """Return ``sql`` with every resolvable qualifier mapped to its alias."""
        out: list[str] = []
        i = 0
        # Skip quoted string literals.
        for match in re.finditer(r"'(?:[^']|'')*'", sql):
            out.append(self._rewrite_code(sql[i : match.start()]))
            out.append(match.group(0))
            i = match.end()
        out.append(self._rewrite_code(sql[i:]))
        return "".join(out)

    def _rewrite_code(self, code: str) -> str:
        def _sub(m: re.Match[str]) -> str:
            qualifier, column = m.group(1), m.group(2)
            entry = self._ctx.entry_for(qualifier, column.strip("`"))
            if entry is not None:
                return f"`{entry.alias}`.{column}"
            return m.group(0)

        return self._QUAL.sub(_sub, code)


class DuckFrame:
    """A lazy, PySpark-``DataFrame``-shaped wrapper over a DuckDB relation."""

    def __init__(
        self,
        engine: Any,
        relation: Any,
        *,
        alias: str | None = None,
        lineage: frozenset[str] = frozenset(),
        scope: tuple[ScopeEntry, ...] | None = None,
    ) -> None:
        """Wrap ``relation``.

        Args:
            engine: Owning :class:`~ovid2.core.engines.duckdb.DuckDBEngine`.
            relation: A lazy ``DuckDBPyRelation``.
            alias: Alias to expose the relation under (fresh if ``None``).
            lineage: Spark-level qualifiers resolving to this relation.
            scope: Explicit multi-relation scope (joins); defaults to the
                single entry for this relation.
        """
        self._engine = engine
        if scope is not None and alias is None:
            # Join results: keep DuckDB's own JOIN_RELATION alias.
            # ``set_alias`` on a join relation hides the child aliases the
            # scope still refers to.
            self._alias = str(relation.alias)
            self._rel = relation
        else:
            self._alias = alias or _fresh_alias()
            self._rel = (
                relation if str(relation.alias) == self._alias else relation.set_alias(self._alias)
            )
        self._lineage = frozenset(lineage) | {self._alias}
        types = {c.lower(): str(t) for c, t in zip(self._rel.columns, self._rel.types, strict=True)}
        self._scope = (
            scope if scope is not None else (ScopeEntry(self._alias, self._lineage, types),)
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def relation(self) -> Any:
        """Return the underlying lazy ``DuckDBPyRelation``."""
        return self._rel

    @property
    def columns(self) -> list[str]:
        """Column names (duplicates allowed after a join)."""
        return list(self._rel.columns)

    @property
    def dtypes(self) -> list[tuple[str, str]]:
        """``(name, duck_type)`` pairs."""
        return [(c, str(t)) for c, t in zip(self._rel.columns, self._rel.types, strict=True)]

    @property
    def sparkSession(self) -> Any:  # noqa: N802 — PySpark API
        """Return the engine session adapter (``createDataFrame`` etc.)."""
        return self._engine.get_session()

    def _ctx(self) -> RenderCtx:
        return RenderCtx(self._scope)

    def _derive(self, relation: Any, *, keep_scope: bool = False) -> DuckFrame:
        """Return a frame over ``relation`` inheriting this frame's lineage."""
        lineage = frozenset().union(*(e.lineage for e in self._scope)) | self._lineage
        if keep_scope:
            return DuckFrame(
                self._engine, relation, alias=self._alias, lineage=lineage, scope=self._scope
            )
        return DuckFrame(self._engine, relation, lineage=lineage)

    def _project(self, items: list[str]) -> Any:
        return self._rel.project(", ".join(items))

    def _expand(self, cols: Iterable[DuckColumn]) -> list[str]:
        ctx = self._ctx()
        return [c.projection_sql(ctx) for c in cols]

    def _sql_string(self, text: str) -> str:
        """Transpile a Spark SQL fragment and resolve its qualifiers."""
        types: dict[str, str] = {}
        for entry in self._scope:
            for name, duck_type in entry.types.items():
                types.setdefault(name, duck_type)
        return transpile_typed(_SqlStringResolver(self._ctx()).rewrite(text), types)

    def __getitem__(self, item: Any) -> Any:
        if isinstance(item, str):
            return DuckColumn.ref(self._alias, item)
        if isinstance(item, int):
            return DuckColumn.ref(self._alias, self.columns[item])
        if isinstance(item, DuckColumn):
            return self.filter(item)
        if isinstance(item, (list, tuple)):
            return self.select(*item)
        raise TypeError(f"Unsupported DuckFrame index {item!r}")

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name.lower() in {c.lower() for c in self._rel.columns}:
            return DuckColumn.ref(self._alias, name)
        raise AttributeError(f"'DuckFrame' object has no attribute {name!r}")

    # ------------------------------------------------------------------
    # Projection
    # ------------------------------------------------------------------

    def select(self, *cols: Any) -> DuckFrame:
        """Spark ``select`` (columns, names, ``"*"``, ``"alias.*"``)."""
        items = self._expand(columns_from_args(cols))
        return self._derive(self._project(items or ["*"]))

    def select_native(self, items: list[str]) -> DuckFrame:
        """Project raw **DuckDB** SQL items (``"<expr> AS <ident>"``), no transpiling.

        For engine-internal fast paths that build trusted SQL from column
        names (never user text): ``#<n>`` refers to the n-th column.
        """
        return self._derive(self._project(items))

    def selectExpr(self, *exprs: str) -> DuckFrame:  # noqa: N802 — PySpark API
        """Spark ``selectExpr`` with Spark SQL strings."""
        items: list[str] = []
        flat: list[str] = []
        for e in exprs:
            flat.extend(e if isinstance(e, (list, tuple)) else [e])
        for text in flat:
            items.append("*" if text.strip() == "*" else self._sql_string(text))
        if any(i == "*" for i in items) and len(items) > 1:
            # ``selectExpr("*", "expr AS x")``: Spark keeps both when x is
            # new and *duplicates* it when x exists; the latter never
            # happens intentionally in Ovid specs (``post_expressions``
            # always add/overwrite), so overwrite like ``withColumn``.
            frame: DuckFrame = self
            for text in flat:
                if text.strip() == "*":
                    continue
                name = _alias_of(text)
                if name is None:
                    frame = frame._derive(frame._project(["*", frame._sql_string(text)]))
                else:
                    frame = frame._with_sql(name, frame._sql_string(_strip_alias(text)))
            return frame
        return self._derive(self._project(items))

    def withColumn(self, name: str, col: Any) -> DuckFrame:  # noqa: N802 — PySpark API
        """Add or replace a column."""
        column = to_column(col, literal_strings=True)
        ctx = self._ctx()
        expr = column.alias(name).projection_sql(ctx).rsplit(" AS ", 1)[0]
        return self._with_sql(name, expr)

    def _with_sql(self, name: str, expr_sql: str) -> DuckFrame:
        lowered = name.lower()
        out: list[str] = []
        replaced = False
        for idx, column in enumerate(self._rel.columns, start=1):
            if column.lower() == lowered:
                if not replaced:
                    out.append(f"{expr_sql} AS {ident(name)}")
                    replaced = True
                continue
            out.append(f"#{idx} AS {ident(column)}")
        if not replaced:
            out.append(f"{expr_sql} AS {ident(name)}")
        return self._derive(self._project(out))

    def withColumns(self, mapping: dict[str, Any]) -> DuckFrame:  # noqa: N802
        """Apply several :meth:`withColumn` in order."""
        frame = self
        for name, col in mapping.items():
            frame = frame.withColumn(name, col)
        return frame

    def withColumnRenamed(self, existing: str, new: str) -> DuckFrame:  # noqa: N802
        """Rename one column (no-op if absent, as in Spark)."""
        lowered = existing.lower()
        if lowered not in {c.lower() for c in self._rel.columns}:
            return self
        items = [
            f"#{i} AS {ident(new if c.lower() == lowered else c)}"
            for i, c in enumerate(self._rel.columns, start=1)
        ]
        return self._derive(self._project(items))

    def toDF(self, *names: str) -> DuckFrame:  # noqa: N802 — PySpark API
        """Rename every column positionally."""
        if len(names) == 1 and isinstance(names[0], (list, tuple)):
            names = tuple(names[0])
        if len(names) != len(self._rel.columns):
            raise ValueError(f"toDF: expected {len(self._rel.columns)} names, got {len(names)}")
        items = [f"#{i} AS {ident(n)}" for i, n in enumerate(names, start=1)]
        return self._derive(self._project(items))

    def drop(self, *cols: Any) -> DuckFrame:
        """Drop columns by name (absent names are ignored, as in Spark)."""
        names: set[str] = set()
        for c in cols:
            if isinstance(c, (list, tuple)):
                names.update(str(x).lower() for x in c)
            elif isinstance(c, DuckColumn):
                if c.output_name:
                    names.add(c.output_name.lower())
            else:
                names.add(str(c).lower())
        keep = [
            f"#{i} AS {ident(c)}"
            for i, c in enumerate(self._rel.columns, start=1)
            if c.lower() not in names
        ]
        if not keep:
            # DuckDB has no zero-column relation; Spark would return a
            # frame with no columns. Callers only ever check ``columns``.
            raise ValueError("drop: cannot drop every column of a DuckDB frame")
        return self._derive(self._project(keep))

    def alias(self, name: str) -> DuckFrame:
        """Expose the frame under the Spark alias ``name``."""
        return DuckFrame(self._engine, self._rel, lineage=self._lineage | {name}, alias=None)

    # ------------------------------------------------------------------
    # Row filters / set ops
    # ------------------------------------------------------------------

    def filter(self, condition: Any) -> DuckFrame:
        """Spark ``filter`` / ``where`` (column or SQL string)."""
        if isinstance(condition, str):
            sql = self._sql_string(condition)
        else:
            sql = to_column(condition).sql(self._ctx())
        return self._derive(self._rel.filter(sql), keep_scope=True)

    where = filter

    def distinct(self) -> DuckFrame:
        """``SELECT DISTINCT *``."""
        return self._derive(self._rel.distinct())

    def dropDuplicates(self, subset: Sequence[str] | None = None) -> DuckFrame:  # noqa: N802
        """Drop duplicate rows (optionally per ``subset``; first row wins)."""
        if not subset:
            return self.distinct()
        keys = ", ".join(ident(k) for k in subset)
        name = _fresh_alias("__ovq")
        rel = self._rel.query(name, f"SELECT DISTINCT ON ({keys}) * FROM {ident(name)}")
        return self._derive(rel)

    drop_duplicates = dropDuplicates

    def dropDuplicatesKeepIncompleteKeys(self, subset: Sequence[str]) -> DuckFrame:  # noqa: N802
        """Dedup on ``subset``; rows with a NULL / ``''`` key part all survive.

        One hash-aggregate pass — no UNION, so the upstream plan runs
        once. Incomplete-key rows get a unique tag (``gen_random_uuid``)
        as an extra ``DISTINCT ON`` key, so each forms its own group;
        complete-key rows share a NULL tag and dedup on ``subset``. A
        hash ``DISTINCT ON`` is ~2x faster than a ``row_number()``
        window here and spills gracefully.
        """
        keys = [ident(k) for k in subset]
        complete = " AND ".join(f"({k} IS NOT NULL AND CAST({k} AS VARCHAR) <> '')" for k in keys)
        tag = ident(_fresh_alias("__ovdk"))
        name = _fresh_alias("__ovq")
        rel = self._rel.query(
            name,
            f"SELECT * EXCLUDE ({tag}) FROM (SELECT DISTINCT ON ({', '.join(keys)}, {tag}) * "
            f"FROM (SELECT *, CASE WHEN {complete} THEN NULL ELSE gen_random_uuid() END "
            f"AS {tag} FROM {ident(name)}))",
        )
        return self._derive(rel)

    def limit(self, n: int) -> DuckFrame:
        """First ``n`` rows."""
        return self._derive(self._rel.limit(int(n)), keep_scope=True)

    def orderBy(self, *cols: Any, ascending: Any = None) -> DuckFrame:  # noqa: N802
        """Sort (Spark null ordering: asc nulls first, desc nulls last)."""
        columns = columns_from_args(cols)
        if ascending is not None:
            flags = (
                ascending if isinstance(ascending, (list, tuple)) else [ascending] * len(columns)
            )
            columns = [
                c.asc() if bool(f) else c.desc() for c, f in zip(columns, flags, strict=True)
            ]
        ctx = self._ctx()
        return self._derive(
            self._rel.order(", ".join(c.order_sql(ctx) for c in columns)), keep_scope=True
        )

    sort = orderBy

    def unionByName(self, other: DuckFrame, allowMissingColumns: bool = False) -> DuckFrame:  # noqa: N802, N803
        """``UNION ALL BY NAME`` (Spark ``unionByName``)."""
        mine = {c.lower() for c in self.columns}
        theirs = {c.lower() for c in other.columns}
        if not allowMissingColumns and mine != theirs:
            missing = sorted(mine ^ theirs)
            raise ValueError(f"unionByName: column sets differ: {missing}")
        left = self._stringified_against(other)
        right = other._stringified_against(self)
        # Output order: left's columns, then right-only columns (Spark).
        order: list[str] = list(left.columns)
        seen = {c.lower() for c in order}
        order += [c for c in right.columns if c.lower() not in seen]
        left_al = left._aligned(order)
        right_al = right._aligned(order)
        return self._derive(left_al._rel.union(right_al._rel))

    def _aligned(self, order: list[str]) -> DuckFrame:
        """Project to ``order`` (case-insensitive), NULL-filling missing columns."""
        position = {c.lower(): i for i, c in enumerate(self.columns, start=1)}
        types = {c.lower(): str(t) for c, t in self.dtypes}
        items: list[str] = []
        for name in order:
            idx = position.get(name.lower())
            if idx is None:
                items.append(f"CAST(NULL AS VARCHAR) AS {ident(name)}")
            elif types[name.lower()] == '"NULL"':
                items.append(f"CAST(#{idx} AS VARCHAR) AS {ident(name)}")
            else:
                items.append(f"#{idx} AS {ident(name)}")
        return self._derive(self._project(items))

    def union(self, other: DuckFrame) -> DuckFrame:
        """Positional ``UNION ALL`` (Spark ``union``)."""
        return self._derive(self._rel.union(other._rel))

    unionAll = union  # noqa: N815 — PySpark API

    def _stringified_against(self, other: DuckFrame) -> DuckFrame:
        """Cast columns to VARCHAR where ``other`` holds a VARCHAR of the same name.

        Spark widens to string in ``unionByName``; DuckDB would try to
        cast the string side to the other type and fail.
        """
        other_types = {c.lower(): str(t) for c, t in other.dtypes}
        items: list[str] = []
        changed = False
        for idx, (c, t) in enumerate(self.dtypes, start=1):
            peer = other_types.get(c.lower())
            if (
                peer is not None and peer.upper() == "VARCHAR" and str(t).upper() != "VARCHAR"
            ) or str(t).upper() == '"NULL"':
                items.append(f"CAST(#{idx} AS VARCHAR) AS {ident(c)}")
                changed = True
            else:
                items.append(f"#{idx} AS {ident(c)}")
        return self._derive(self._project(items)) if changed else self

    # ------------------------------------------------------------------
    # Joins
    # ------------------------------------------------------------------

    def join(self, other: DuckFrame, on: Any = None, how: str | None = None) -> DuckFrame:
        """Spark ``join`` (column predicate, SQL string, name or name list)."""
        how_norm = _JOIN_TYPES.get((how or "inner").lower().replace("_", ""))
        if how_norm is None:
            raise ValueError(f"Unsupported join type {how!r}")
        right = other if other._alias != self._alias else other._derive(other._rel)
        scope = (*self._scope, *right._scope)
        ctx = RenderCtx(scope)

        if on is None:
            if how_norm != "inner":
                raise ValueError("join without 'on' is only valid as a cross join")
            return self.crossJoin(other)

        names: list[str] | None = None
        if isinstance(on, str) and not _looks_like_sql(on):
            names = [on]
        elif isinstance(on, (list, tuple)) and on and all(isinstance(x, str) for x in on):
            names = list(on)

        if names is not None:
            return self._using_join(right, names, how_norm)

        if isinstance(on, (list, tuple)):
            preds = [to_column(p) for p in on]
            condition = preds[0]
            for p in preds[1:]:
                condition = condition & p
            cond_sql = condition.sql(ctx)
        elif isinstance(on, str):
            cond_sql = transpile(_SqlStringResolver(ctx).rewrite(on))
        else:
            cond_sql = to_column(on).sql(ctx)

        rel = self._rel.join(right._rel, cond_sql, how=how_norm)
        if how_norm in ("semi", "anti"):
            return DuckFrame(
                self._engine,
                rel,
                alias=self._alias,
                lineage=self._lineage,
                scope=self._scope,
            )
        lineage = self._lineage | right._lineage
        return DuckFrame(self._engine, rel, lineage=lineage, scope=scope)

    def _using_join(self, right: DuckFrame, names: list[str], how: str) -> DuckFrame:
        cond = " AND ".join(
            f"{ident(self._alias_for_key(n))}.{ident(n)} = {ident(right._alias_for_key(n))}.{ident(n)}"
            for n in names
        )
        rel = self._rel.join(right._rel, cond, how=how)
        if how in ("semi", "anti"):
            return DuckFrame(
                self._engine, rel, alias=self._alias, lineage=self._lineage, scope=self._scope
            )
        # Spark ``USING`` semantics: one key column (coalesced for outer
        # joins) followed by the remaining left then right columns.
        lowered = {n.lower() for n in names}
        left_a = {e.alias for e in self._scope}
        items: list[str] = []
        for n in names:
            l_ref = f"{ident(self._alias_for_key(n))}.{ident(n)}"
            r_ref = f"{ident(right._alias_for_key(n))}.{ident(n)}"
            if how == "inner" or how == "left":
                items.append(f"{l_ref} AS {ident(n)}")
            elif how == "right":
                items.append(f"{r_ref} AS {ident(n)}")
            else:
                items.append(f"COALESCE({l_ref}, {r_ref}) AS {ident(n)}")
        n_left = len(self.columns)
        for idx, c in enumerate(self.columns, start=1):
            if c.lower() not in lowered:
                items.append(f"#{idx} AS {ident(c)}")
        for idx, c in enumerate(right.columns, start=1):
            if c.lower() not in lowered:
                items.append(f"#{n_left + idx} AS {ident(c)}")
        del left_a
        projected = rel.project(", ".join(items))
        lineage = self._lineage | right._lineage
        return DuckFrame(self._engine, projected, lineage=lineage)

    def _alias_for_key(self, name: str) -> str:
        lowered = name.lower()
        for entry in self._scope:
            if lowered in entry.types:
                return entry.alias
        return self._alias

    def crossJoin(self, other: DuckFrame) -> DuckFrame:  # noqa: N802 — PySpark API
        """Cartesian product."""
        right = other if other._alias != self._alias else other._derive(other._rel)
        rel = self._rel.cross(right._rel)
        scope = (*self._scope, *right._scope)
        return DuckFrame(self._engine, rel, lineage=self._lineage | right._lineage, scope=scope)

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def groupBy(self, *cols: Any) -> DuckGroupedData:  # noqa: N802 — PySpark API
        """Return a grouped handle (``.agg`` / ``.count``)."""
        return DuckGroupedData(self, columns_from_args(cols))

    groupby = groupBy

    def agg(self, *exprs: Any) -> DuckFrame:
        """Global aggregation."""
        return DuckGroupedData(self, []).agg(*exprs)

    # ------------------------------------------------------------------
    # Views / caching
    # ------------------------------------------------------------------

    def createOrReplaceTempView(self, name: str) -> None:  # noqa: N802 — PySpark API
        """Register the frame under ``name`` for ``session.table(name)``."""
        self._engine.register_view(name, self)

    createTempView = createOrReplaceTempView  # noqa: N815 — PySpark API
    create_or_replace_temp_view = createOrReplaceTempView

    def cache(self) -> DuckFrame:
        """Materialise into a temp table (engine checkpoint)."""
        return self._engine.materialise(self)  # type: ignore[no-any-return]

    def persist(self, *_args: Any, **_kwargs: Any) -> DuckFrame:
        """Alias of :meth:`cache`."""
        return self.cache()

    def unpersist(self, blocking: bool = False) -> DuckFrame:
        """Drop the backing temp table, if any."""
        del blocking
        self._engine.release(self)
        return self

    # ------------------------------------------------------------------
    # Sampling
    # ------------------------------------------------------------------

    def sample(
        self,
        withReplacement: bool | None = None,  # noqa: N803 — PySpark API
        fraction: float | None = None,
        seed: int | None = None,
    ) -> DuckFrame:
        """Bernoulli sample of ``fraction`` of the rows."""
        if isinstance(withReplacement, float) and fraction is None:
            fraction, withReplacement = withReplacement, False  # noqa: N806
        if withReplacement:
            from ovid2.core.engines.protocols import EngineOperationUnsupported

            raise EngineOperationUnsupported("duckdb", "sample(withReplacement=True)")
        pct = max(0.0, min(100.0, float(fraction or 0.0) * 100.0))
        name = _fresh_alias("__ovq")
        repeat = f" REPEATABLE ({int(seed)})" if seed is not None else ""
        rel = self._rel.query(
            name, f"SELECT * FROM {ident(name)} USING SAMPLE BERNOULLI ({pct} PERCENT){repeat}"
        )
        return self._derive(rel)

    # ------------------------------------------------------------------
    # Portable surface (``ovid2.core.engines.surface.DATAFRAME_METHODS``)
    # ------------------------------------------------------------------

    @property
    def schema(self) -> Any:
        """Return a portable :class:`~ovid2.core.engines.types.StructType`."""
        from ovid2.core.engines.types import StructField, StructType

        return StructType(
            [StructField(name, _spark_type_of(duck_type)) for name, duck_type in self.dtypes]
        )

    def printSchema(self) -> None:  # noqa: N802 — PySpark API
        """Print the schema tree (diagnostics)."""
        lines = ["root"] + [
            f" |-- {f.name}: {f.dataType.simpleString()} (nullable = true)" for f in self.schema
        ]
        print("\n".join(lines))  # noqa: T201 — PySpark API prints by contract

    def show(self, n: int = 20, truncate: Any = True, vertical: bool = False) -> None:
        """Print the first ``n`` rows (diagnostics)."""
        del truncate, vertical
        print(self._rel.limit(int(n)))  # noqa: T201 — PySpark API prints by contract

    def hint(self, *_args: Any, **_kwargs: Any) -> DuckFrame:
        """Join hints are planner advice on Spark; DuckDB plans joins itself."""
        return self

    def repartition(self, *_args: Any, **_kwargs: Any) -> DuckFrame:
        """Partitioning is a Spark execution detail; a no-op here."""
        return self

    def coalesce(self, *_args: Any) -> DuckFrame:
        """Partition count is a Spark execution detail; a no-op here."""
        return self

    def transform(self, func: Any, *args: Any, **kwargs: Any) -> DuckFrame:
        """``df.transform(f)`` → ``f(df)``."""
        return func(self, *args, **kwargs)  # type: ignore[no-any-return]

    def checkpoint(self, eager: bool = True) -> DuckFrame:
        """Materialise into scratch Parquet (Spark ``checkpoint``)."""
        del eager
        return self.cache()

    localCheckpoint = checkpoint  # noqa: N815 — PySpark API

    def subtract(self, other: DuckFrame) -> DuckFrame:
        """``EXCEPT DISTINCT`` (Spark ``subtract``)."""
        return self._derive(self._rel.except_(other._aligned(self.columns)._rel).distinct())

    def exceptAll(self, other: DuckFrame) -> DuckFrame:  # noqa: N802 — PySpark API
        """``EXCEPT ALL``."""
        name_l, name_r = _fresh_alias("__ovq"), _fresh_alias("__ovr")
        right = other._aligned(self.columns)
        right._rel.create_view(name_r, replace=True)
        rel = self._rel.query(
            name_l, f"SELECT * FROM {ident(name_l)} EXCEPT ALL SELECT * FROM {ident(name_r)}"
        )
        return self._derive(rel)

    def intersect(self, other: DuckFrame) -> DuckFrame:
        """``INTERSECT DISTINCT``."""
        return self._derive(self._rel.intersect(other._aligned(self.columns)._rel).distinct())

    def intersectAll(self, other: DuckFrame) -> DuckFrame:  # noqa: N802 — PySpark API
        """``INTERSECT ALL``."""
        name_l, name_r = _fresh_alias("__ovq"), _fresh_alias("__ovr")
        right = other._aligned(self.columns)
        right._rel.create_view(name_r, replace=True)
        rel = self._rel.query(
            name_l, f"SELECT * FROM {ident(name_l)} INTERSECT ALL SELECT * FROM {ident(name_r)}"
        )
        return self._derive(rel)

    def fillna(self, value: Any, subset: Any = None) -> DuckFrame:
        """Spark ``fillna``: replace NULLs in type-compatible columns."""
        if isinstance(value, dict):
            mapping = {str(k).lower(): v for k, v in value.items()}
        else:
            targets = (
                [subset] if isinstance(subset, str) else list(subset) if subset else self.columns
            )
            mapping = {str(c).lower(): value for c in targets}
        items: list[str] = []
        for idx, (name, duck_type) in enumerate(self.dtypes, start=1):
            fill = mapping.get(name.lower(), _NO_FILL)
            if fill is not _NO_FILL and _fill_applies(fill, duck_type):
                lit_sql = DuckColumn.literal(fill).sql(self._ctx())
                items.append(f"COALESCE(#{idx}, CAST({lit_sql} AS {duck_type})) AS {ident(name)}")
            else:
                items.append(f"#{idx} AS {ident(name)}")
        return self._derive(self._project(items))

    def dropna(self, how: str = "any", thresh: int | None = None, subset: Any = None) -> DuckFrame:
        """Spark ``dropna``."""
        cols = [subset] if isinstance(subset, str) else list(subset) if subset else self.columns
        refs = [ident(c) for c in cols]
        non_null = " + ".join(f"CASE WHEN {r} IS NOT NULL THEN 1 ELSE 0 END" for r in refs) or "0"
        if thresh is not None:
            cond = f"({non_null}) >= {int(thresh)}"
        elif how == "all":
            cond = f"({non_null}) > 0"
        else:
            cond = f"({non_null}) = {len(refs)}"
        return self._derive(self._rel.filter(cond), keep_scope=True)

    def replace(self, to_replace: Any, value: Any = None, subset: Any = None) -> DuckFrame:
        """Spark ``replace`` for scalar / list / dict replacements."""
        if isinstance(to_replace, dict):
            pairs = list(to_replace.items())
        elif isinstance(to_replace, (list, tuple)):
            values = value if isinstance(value, (list, tuple)) else [value] * len(to_replace)
            pairs = list(zip(to_replace, values, strict=True))
        else:
            pairs = [(to_replace, value)]
        targets = {
            c.lower()
            for c in (
                [subset] if isinstance(subset, str) else list(subset) if subset else self.columns
            )
        }
        ctx = self._ctx()
        items: list[str] = []
        for idx, (name, duck_type) in enumerate(self.dtypes, start=1):
            usable = [(a, b) for a, b in pairs if _fill_applies(a, duck_type)]
            if name.lower() not in targets or not usable:
                items.append(f"#{idx} AS {ident(name)}")
                continue
            whens = " ".join(
                f"WHEN #{idx} = {DuckColumn.literal(a).sql(ctx)} THEN "
                f"CAST({DuckColumn.literal(b).sql(ctx)} AS {duck_type})"
                for a, b in usable
            )
            items.append(f"CASE {whens} ELSE #{idx} END AS {ident(name)}")
        return self._derive(self._project(items))

    @property
    def na(self) -> _DuckNaFunctions:
        """``df.na.fill`` / ``df.na.drop`` / ``df.na.replace``."""
        return _DuckNaFunctions(self)

    @property
    def stat(self) -> _DuckStat:
        """``df.stat`` (``sampleBy``)."""
        return _DuckStat(self)

    def sampleBy(self, col: Any, fractions: dict[Any, float], seed: int | None = None) -> DuckFrame:  # noqa: N802
        """Stratified Bernoulli sample: keep each row with its key's fraction."""
        return _DuckStat(self).sampleBy(col, fractions, seed)

    @property
    def rdd(self) -> _DuckRdd:
        """A small, eager ``RDD`` stand-in (``map`` / ``collect`` / ``flatMap``)."""
        return _DuckRdd(self._fetch_rows)

    @property
    def write(self) -> _DuckWriter:
        """Spark ``DataFrameWriter`` for *local paths* (Parquet / CSV / text / JSON).

        Pipeline output goes through the engine stager (any storage type);
        this writer serves code that writes scratch files itself (legacy
        fix-chain checkpoints, diagnostics).
        """
        return _DuckWriter(self)

    # ------------------------------------------------------------------
    # Actions (explicit; never called by lazy pipeline code)
    # ------------------------------------------------------------------

    def collect(self) -> list[DuckRow]:
        """Execute and return every row."""
        return self._fetch_rows()

    def _fetch_rows(self) -> list[DuckRow]:
        fields = self.columns
        return [DuckRow(r, fields) for r in self._rel.fetchall()]

    def count(self) -> int:
        """Execute ``count(*)``."""
        row = self._rel.aggregate("count(*)").fetchone()
        return int(row[0]) if row else 0

    def first(self) -> DuckRow | None:
        """Execute and return the first row (``None`` when empty)."""
        row = self._rel.limit(1).fetchone()
        return DuckRow(row, self.columns) if row is not None else None

    head = first

    def take(self, n: int) -> list[DuckRow]:
        """Execute and return the first ``n`` rows."""
        fields = self.columns
        return [DuckRow(r, fields) for r in self._rel.limit(int(n)).fetchall()]

    def isEmpty(self) -> bool:  # noqa: N802 — PySpark API
        """Return ``True`` when the frame has no rows."""
        return self._rel.limit(1).fetchone() is None

    def toPandas(self) -> Any:  # noqa: N802 — PySpark API
        """Execute into a pandas DataFrame."""
        return self._rel.df()

    def to_arrow(self) -> Any:
        """Execute into a ``pyarrow.Table``.

        DuckDB >= 1.4 made ``relation.arrow()`` return a
        ``RecordBatchReader``; ``fetch_arrow_table`` is a ``Table`` on every
        supported version.
        """
        return self._rel.fetch_arrow_table()

    def to_arrow_batches(self, batch_size: int = 65_536) -> Any:
        """Execute and stream ``pyarrow.RecordBatch`` es (bounded memory).

        The reader is always closed, also when the consumer stops early or
        the query is interrupted: an open DuckDB reader pins its pending
        query, and with it the connection's catalog (a later
        ``create_function`` of a just-removed UDF then fails with
        "already exists").
        """
        to_reader = getattr(self._rel, "to_arrow_reader", None)
        reader = to_reader(batch_size) if callable(to_reader) else None
        if reader is None:
            reader = self._rel.fetch_record_batch(batch_size)
        try:
            while True:
                try:
                    yield reader.read_next_batch()
                except StopIteration:
                    return
        finally:
            with contextlib.suppress(Exception):
                reader.close()

    def explain(self, *_args: Any) -> str:
        """Return DuckDB's physical plan."""
        return str(self._rel.explain())

    def sql_query(self) -> str:
        """Return the SQL text of the lazy relation (diagnostics)."""
        return str(self._rel.sql_query())

    def __repr__(self) -> str:
        return f"DuckFrame[{', '.join(f'{c}: {t}' for c, t in self.dtypes)}]"


_JOIN_TYPES: dict[str, str] = {
    "inner": "inner",
    "cross": "inner",
    "left": "left",
    "leftouter": "left",
    "right": "right",
    "rightouter": "right",
    "outer": "outer",
    "full": "outer",
    "fullouter": "outer",
    "leftsemi": "semi",
    "semi": "semi",
    "leftanti": "anti",
    "anti": "anti",
}


def _looks_like_sql(text: str) -> bool:
    return bool(re.search(r"[=<>()\s]", text))


_ALIAS_RE = re.compile(r"\s+(?:AS\s+)?(`[^`]+`|[A-Za-z_][A-Za-z0-9_]*)\s*$", re.IGNORECASE)


def _alias_of(text: str) -> str | None:
    """Return the trailing ``AS name`` of a select expression, if any."""
    match = re.search(r"\s+AS\s+(`[^`]+`|[A-Za-z_][A-Za-z0-9_]*)\s*$", text, re.IGNORECASE)
    if match is None:
        return None
    return match.group(1).strip("`")


def _strip_alias(text: str) -> str:
    return re.sub(r"\s+AS\s+(`[^`]+`|[A-Za-z_][A-Za-z0-9_]*)\s*$", "", text, flags=re.IGNORECASE)


class DuckGroupedData:
    """Result of :meth:`DuckFrame.groupBy` (optionally pivoted)."""

    def __init__(
        self,
        frame: DuckFrame,
        keys: list[DuckColumn],
        pivot: tuple[DuckColumn, list[Any]] | None = None,
    ) -> None:
        self._frame = frame
        self._keys = keys
        self._pivot = pivot

    def _items(self, exprs: tuple[Any, ...]) -> list[DuckColumn]:
        if len(exprs) == 1 and isinstance(exprs[0], dict):
            return [
                DuckColumn.raw(f"{fn_name}({ident(column)})", name=f"{fn_name}({column})")
                for column, fn_name in exprs[0].items()
            ]
        return columns_from_args(exprs)

    def agg(self, *exprs: Any) -> DuckFrame:
        """Aggregate with columns (``max(c).alias(...)``) or a ``{col: fn}`` dict."""
        ctx = self._frame._ctx()
        items = self._items(exprs)
        if self._pivot is not None:
            return self._pivot_agg(items, ctx)
        key_sql = [k.projection_sql(ctx) for k in self._keys]
        agg_sql = [c.projection_sql(ctx) for c in items]
        group_sql = ", ".join(k.sql(ctx) for k in self._keys)
        rel = self._frame._rel.aggregate(", ".join(key_sql + agg_sql), group_sql)
        return self._frame._derive(rel)

    def _pivot_agg(self, items: list[DuckColumn], ctx: RenderCtx) -> DuckFrame:
        """Spark pivot: one output column per (value[, aggregate]) via ``FILTER``."""
        pivot_col, values = self._pivot  # type: ignore[misc]
        pivot_sql = pivot_col.sql(ctx)
        key_sql = [k.projection_sql(ctx) for k in self._keys]
        cols: list[str] = []
        for value in values:
            label = "null" if value is None else str(value)
            cond = (
                f"{pivot_sql} IS NULL"
                if value is None
                else f"{pivot_sql} = {DuckColumn.literal(value).sql(ctx)}"
            )
            for item in items:
                inner = item.sql(ctx)
                name = label if len(items) == 1 else f"{label}_{item.output_name or inner}"
                filtered = re.sub(r"\)\s*$", f") FILTER (WHERE {cond})", inner, count=1)
                cols.append(f"{filtered} AS {ident(name)}")
        group_sql = ", ".join(k.sql(ctx) for k in self._keys)
        rel = self._frame._rel.aggregate(", ".join(key_sql + cols), group_sql)
        return self._frame._derive(rel)

    def pivot(self, pivot_col: Any, values: list[Any] | None = None) -> DuckGroupedData:
        """Spark ``pivot``; without ``values`` the distinct values are collected (as Spark does)."""
        column = to_column(pivot_col)
        if values is None:
            ctx = self._frame._ctx()
            rel = self._frame._rel.project(f"{column.sql(ctx)} AS __pv").distinct().order("__pv")
            values = [r[0] for r in rel.fetchall()]  # Spark pivot without values runs the same job
        return DuckGroupedData(self._frame, self._keys, (column, list(values)))

    def count(self) -> DuckFrame:
        """``count(*)`` per group as column ``count``."""
        return self.agg(DuckColumn.raw("count(*)", "num", name="count"))

    def _simple(self, fn: str, cols: Sequence[str]) -> DuckFrame:
        return self.agg(*[DuckColumn.raw(f"{fn}({ident(c)})", name=f"{fn}({c})") for c in cols])

    def max(self, *cols: str) -> DuckFrame:
        """Per-group max."""
        return self._simple("max", cols)

    def min(self, *cols: str) -> DuckFrame:
        """Per-group min."""
        return self._simple("min", cols)

    def sum(self, *cols: str) -> DuckFrame:
        """Per-group sum."""
        return self._simple("sum", cols)

    def avg(self, *cols: str) -> DuckFrame:
        """Per-group mean."""
        return self._simple("avg", cols)

    mean = avg


class DuckWindowSpec:
    """``Window`` spec (``partitionBy`` / ``orderBy`` / ``rowsBetween`` / ``rangeBetween``)."""

    def __init__(
        self,
        partition: list[DuckColumn] | None = None,
        order: list[DuckColumn] | None = None,
        frame: str | None = None,
    ) -> None:
        self._partition = partition or []
        self._order = order or []
        self._frame = frame

    def partitionBy(self, *cols: Any) -> DuckWindowSpec:  # noqa: N802
        """Set the partition keys."""
        return DuckWindowSpec(columns_from_args(cols), self._order, self._frame)

    def orderBy(self, *cols: Any) -> DuckWindowSpec:  # noqa: N802
        """Set the ordering."""
        return DuckWindowSpec(self._partition, columns_from_args(cols), self._frame)

    def rowsBetween(self, start: int, end: int) -> DuckWindowSpec:  # noqa: N802
        """``ROWS BETWEEN start AND end`` (Spark offsets; ``Window.unbounded*``)."""
        return DuckWindowSpec(self._partition, self._order, "ROWS " + _frame_bounds(start, end))

    def rangeBetween(self, start: int, end: int) -> DuckWindowSpec:  # noqa: N802
        """``RANGE BETWEEN start AND end``."""
        return DuckWindowSpec(self._partition, self._order, "RANGE " + _frame_bounds(start, end))

    def sql(self, ctx: RenderCtx) -> str:
        """Render the ``OVER (...)`` body."""
        parts: list[str] = []
        if self._partition:
            parts.append("PARTITION BY " + ", ".join(c.sql(ctx) for c in self._partition))
        if self._order:
            parts.append("ORDER BY " + ", ".join(c.order_sql(ctx) for c in self._order))
        if self._frame:
            parts.append(self._frame)
        return " ".join(parts)


#: Spark ``Window.unboundedPreceding`` / ``unboundedFollowing`` / ``currentRow``.
UNBOUNDED_PRECEDING: int = -(1 << 63)
UNBOUNDED_FOLLOWING: int = (1 << 63) - 1
CURRENT_ROW: int = 0


def _frame_bound(value: int, *, start: bool) -> str:
    v = int(value)
    if v <= -(1 << 62):
        return "UNBOUNDED PRECEDING"
    if v >= (1 << 62):
        return "UNBOUNDED FOLLOWING"
    if v == 0:
        return "CURRENT ROW"
    del start
    return f"{-v} PRECEDING" if v < 0 else f"{v} FOLLOWING"


def _frame_bounds(start: int, end: int) -> str:
    return f"BETWEEN {_frame_bound(start, start=True)} AND {_frame_bound(end, start=False)}"


class DuckWindow:
    """``Window`` entry point (``DuckWindow.partitionBy(...)``)."""

    unboundedPreceding = UNBOUNDED_PRECEDING  # noqa: N815 — PySpark API
    unboundedFollowing = UNBOUNDED_FOLLOWING  # noqa: N815 — PySpark API
    currentRow = CURRENT_ROW  # noqa: N815 — PySpark API

    @staticmethod
    def partitionBy(*cols: Any) -> DuckWindowSpec:  # noqa: N802
        """Start a window spec with partition keys."""
        return DuckWindowSpec().partitionBy(*cols)

    @staticmethod
    def orderBy(*cols: Any) -> DuckWindowSpec:  # noqa: N802
        """Start a window spec with an ordering."""
        return DuckWindowSpec().orderBy(*cols)

    @staticmethod
    def rowsBetween(start: int, end: int) -> DuckWindowSpec:  # noqa: N802
        """Start a window spec with a ROWS frame."""
        return DuckWindowSpec().rowsBetween(start, end)

    @staticmethod
    def rangeBetween(start: int, end: int) -> DuckWindowSpec:  # noqa: N802
        """Start a window spec with a RANGE frame."""
        return DuckWindowSpec().rangeBetween(start, end)


_NO_FILL = object()

#: DuckDB type prefix -> Spark simple type (``DuckFrame.schema``).
_DUCK_TO_SPARK: tuple[tuple[str, str], ...] = (
    ("VARCHAR", "string"),
    ("BIGINT", "bigint"),
    ("HUGEINT", "decimal(38,0)"),
    ("INTEGER", "int"),
    ("SMALLINT", "smallint"),
    ("TINYINT", "tinyint"),
    ("DOUBLE", "double"),
    ("FLOAT", "float"),
    ("BOOLEAN", "boolean"),
    ("TIMESTAMP", "timestamp"),
    ("DATE", "date"),
    ("BLOB", "binary"),
)


def _spark_type_of(duck_type: str) -> Any:
    from ovid2.core.engines.types import _as_type

    upper = duck_type.upper()
    if upper.startswith("DECIMAL"):
        return _as_type(upper.lower())
    for prefix, spark in _DUCK_TO_SPARK:
        if upper.startswith(prefix):
            return _as_type(spark)
    if upper == '"NULL"':
        return _as_type("void")
    return _as_type("string")


def _fill_applies(value: Any, duck_type: str) -> bool:
    """Spark ``fillna`` only fills columns whose type matches the value's type."""
    upper = duck_type.upper()
    if isinstance(value, bool):
        return upper == "BOOLEAN"
    if isinstance(value, (int, float)):
        return any(
            upper.startswith(t)
            for t in (
                "INT",
                "BIGINT",
                "SMALLINT",
                "TINYINT",
                "HUGEINT",
                "DOUBLE",
                "FLOAT",
                "DECIMAL",
            )
        )
    if isinstance(value, str):
        return upper.startswith("VARCHAR")
    return value is None


class _DuckStat:
    """``DataFrame.stat`` namespace."""

    def __init__(self, frame: DuckFrame) -> None:
        self._frame = frame

    def sampleBy(self, col: Any, fractions: dict[Any, float], seed: int | None = None) -> DuckFrame:  # noqa: N802
        """Keep each row with probability ``fractions[key]`` (0 for unlisted keys)."""
        frame = self._frame
        key = to_column(col).sql(frame._ctx())
        whens = " ".join(
            f"WHEN CAST({key} AS VARCHAR) = {DuckColumn.literal(str(k)).sql(frame._ctx())} "
            f"THEN {float(v)}"
            for k, v in fractions.items()
        )
        rnd = (
            f"(hash(*COLUMNS(*), {int(seed)}) % 1000000) / 1000000.0"
            if seed is not None
            else "random()"
        )
        cond = f"{rnd} < (CASE {whens} ELSE 0.0 END)" if whens else "FALSE"
        return frame._derive(frame._rel.filter(cond), keep_scope=True)


class _DuckNaFunctions:
    """``DataFrame.na`` namespace."""

    def __init__(self, frame: DuckFrame) -> None:
        self._frame = frame

    def fill(self, value: Any, subset: Any = None) -> DuckFrame:
        """Alias of :meth:`DuckFrame.fillna`."""
        return self._frame.fillna(value, subset)

    def drop(self, how: str = "any", thresh: int | None = None, subset: Any = None) -> DuckFrame:
        """Alias of :meth:`DuckFrame.dropna`."""
        return self._frame.dropna(how, thresh, subset)

    def replace(self, to_replace: Any, value: Any = None, subset: Any = None) -> DuckFrame:
        """Alias of :meth:`DuckFrame.replace`."""
        return self._frame.replace(to_replace, value, subset)


class _DuckRdd:
    """Eager, driver-side ``RDD`` stand-in for small lookup tables.

    Spark code in legacy ports builds Python dicts with
    ``dict(df.rdd.map(lambda r: (r[0], r[1])).collect())``. This class
    supports exactly that family (``map`` / ``flatMap`` / ``filter`` /
    ``collect`` / ``collectAsMap`` / ``count`` / ``take`` / ``isEmpty``):
    the frame is collected once, on the first terminal call.
    """

    def __init__(self, source: Any, steps: tuple[tuple[str, Any], ...] = ()) -> None:
        self._source = source
        self._steps = steps

    def _chain(self, kind: str, fn: Any) -> _DuckRdd:
        return _DuckRdd(self._source, (*self._steps, (kind, fn)))

    def map(self, fn: Any) -> _DuckRdd:
        """Lazy ``map``."""
        return self._chain("map", fn)

    def flatMap(self, fn: Any) -> _DuckRdd:  # noqa: N802 — PySpark API
        """Lazy ``flatMap``."""
        return self._chain("flat", fn)

    def filter(self, fn: Any) -> _DuckRdd:
        """Lazy ``filter``."""
        return self._chain("filter", fn)

    def mapPartitions(self, fn: Any) -> _DuckRdd:  # noqa: N802 — PySpark API
        """One partition: ``fn(iter(rows))``."""
        return self._chain("parts", fn)

    def _rows(self) -> list[Any]:
        rows: list[Any] = list(self._source())
        for kind, fn in self._steps:
            if kind == "map":
                rows = [fn(r) for r in rows]
            elif kind == "flat":
                rows = [x for r in rows for x in fn(r)]
            elif kind == "filter":
                rows = [r for r in rows if fn(r)]
            else:
                rows = list(fn(iter(rows)))
        return rows

    def collect(self) -> list[Any]:
        """Run the chain and return the rows."""
        return self._rows()

    def collectAsMap(self) -> dict[Any, Any]:  # noqa: N802 — PySpark API
        """``dict(collect())``."""
        return dict(self._rows())

    def count(self) -> int:
        """Number of rows."""
        return len(self._rows())

    def take(self, n: int) -> list[Any]:
        """First ``n`` rows."""
        return self._rows()[: int(n)]

    def first(self) -> Any:
        """First row."""
        rows = self._rows()[:1]
        return rows[0] if rows else None

    def isEmpty(self) -> bool:  # noqa: N802 — PySpark API
        """Whether there are no rows."""
        return not self._rows()[:1]

    def getNumPartitions(self) -> int:  # noqa: N802 — PySpark API
        """Always one partition."""
        return 1


class _DuckWriter:
    """Local-path ``DataFrameWriter`` (Spark part-directory layout)."""

    def __init__(self, frame: DuckFrame) -> None:
        self._frame = frame
        self._mode = "errorifexists"
        self._format = "parquet"
        self._options: dict[str, Any] = {}

    def mode(self, mode: str) -> _DuckWriter:
        self._mode = str(mode).lower()
        return self

    def format(self, fmt: str) -> _DuckWriter:
        self._format = str(fmt).lower()
        return self

    def option(self, key: str, value: Any) -> _DuckWriter:
        self._options[str(key).lower()] = value
        return self

    def options(self, **kwargs: Any) -> _DuckWriter:
        for k, v in kwargs.items():
            self.option(k, v)
        return self

    def partitionBy(self, *_cols: Any) -> _DuckWriter:  # noqa: N802 — PySpark API
        return self

    def save(
        self, path: str | None = None, format: str | None = None, mode: str | None = None
    ) -> None:
        if format:
            self._format = format.lower()
        if mode:
            self._mode = mode.lower()
        self._write(str(path))

    def parquet(self, path: str, mode: str | None = None, compression: str | None = None) -> None:
        if compression:
            self._options["compression"] = compression
        self._format = "parquet"
        self.save(path, mode=mode)

    def csv(self, path: str, mode: str | None = None, **kwargs: Any) -> None:
        self.options(**{k: v for k, v in kwargs.items() if v is not None})
        self._format = "csv"
        self.save(path, mode=mode)

    def text(self, path: str) -> None:
        self._format = "text"
        self.save(path)

    def json(self, path: str, mode: str | None = None) -> None:
        self._format = "json"
        self.save(path, mode=mode)

    def _write(self, path: str) -> None:
        import os
        import shutil
        import uuid

        from ovid2.core.engines.duckdb_stager import write_csv_file

        engine = self._frame._engine
        if os.path.exists(path):
            if self._mode in ("overwrite",):
                shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.remove(path)
            elif self._mode == "ignore":
                return
            elif self._mode != "append":
                raise FileExistsError(f"[PATH_ALREADY_EXISTS] Path {path} already exists.")
        os.makedirs(path, exist_ok=True)
        part = f"part-{uuid.uuid4().hex[:12]}"
        if self._format == "csv":
            header = str(self._options.get("header", "false")).lower() == "true"
            delimiter = str(self._options.get("sep") or self._options.get("delimiter") or ",")
            write_csv_file(
                engine,
                self._frame,
                os.path.join(path, part + ".csv"),
                delimiter=delimiter,
                header=header,
            )
        elif self._format == "text":
            from ovid2.legacy.text_io import write_text_file

            write_text_file(self._frame, path, part + ".txt", header=False)
        else:
            from ovid2.core.engines._java_datetime import sql_string

            ext = "parquet" if self._format == "parquet" else "json"
            target = os.path.join(path, f"{part}.{ext}")
            view = f"__ovid2_w_{uuid.uuid4().hex[:8]}"
            self._frame.relation.create_view(view, replace=True)
            codec = str(self._options.get("compression") or "zstd")
            opts = (
                f"FORMAT parquet, COMPRESSION {sql_string(codec)}"
                if ext == "parquet"
                else "FORMAT json"
            )
            conn = engine.connection()
            try:
                conn.execute(f"COPY (SELECT * FROM {ident(view)}) TO {sql_string(target)} ({opts})")
            finally:
                conn.execute(f"DROP VIEW IF EXISTS {ident(view)}")
        open(os.path.join(path, "_SUCCESS"), "wb").close()
