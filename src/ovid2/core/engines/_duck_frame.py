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

    @property
    def write(self) -> Any:
        """Unsupported: DuckDB frames are written through the stager."""
        from ovid2.core.engines.protocols import EngineOperationUnsupported

        raise EngineOperationUnsupported(
            "duckdb", "DataFrame.write", "persist through engine.get_stager().persist(...)"
        )

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

        One ``QUALIFY`` pass — no UNION, so the upstream plan runs once.
        """
        keys = [ident(k) for k in subset]
        complete = " AND ".join(f"({k} IS NOT NULL AND CAST({k} AS VARCHAR) <> '')" for k in keys)
        name = _fresh_alias("__ovq")
        rel = self._rel.query(
            name,
            f"SELECT * FROM {ident(name)} QUALIFY NOT ({complete}) OR "
            f"row_number() OVER (PARTITION BY {', '.join(keys)}) = 1",
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
    # Actions (explicit; never called by lazy pipeline code)
    # ------------------------------------------------------------------

    def collect(self) -> list[DuckRow]:
        """Execute and return every row."""
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
        """Execute into a ``pyarrow.Table``."""
        return self._rel.arrow()

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
    """Result of :meth:`DuckFrame.groupBy`."""

    def __init__(self, frame: DuckFrame, keys: list[DuckColumn]) -> None:
        self._frame = frame
        self._keys = keys

    def agg(self, *exprs: Any) -> DuckFrame:
        """Aggregate with columns (``max(c).alias(...)``) or a ``{col: fn}`` dict."""
        ctx = self._frame._ctx()
        items: list[DuckColumn] = []
        if len(exprs) == 1 and isinstance(exprs[0], dict):
            for column, fn_name in exprs[0].items():
                items.append(
                    DuckColumn.raw(f"{fn_name}({ident(column)})", name=f"{fn_name}({column})")
                )
        else:
            items = columns_from_args(exprs)
        key_sql = [k.projection_sql(ctx) for k in self._keys]
        agg_sql = [c.projection_sql(ctx) for c in items]
        group_sql = ", ".join(k.sql(ctx) for k in self._keys)
        rel = self._frame._rel.aggregate(", ".join(key_sql + agg_sql), group_sql)
        return self._frame._derive(rel)

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

    def pivot(self, *_args: Any, **_kwargs: Any) -> Any:
        """Unsupported on DuckDB (``pivot`` transform is Spark-gated)."""
        from ovid2.core.engines.protocols import EngineOperationUnsupported

        raise EngineOperationUnsupported("duckdb", "GroupedData.pivot")


class DuckWindowSpec:
    """Minimal ``Window`` spec (``partitionBy`` / ``orderBy``)."""

    def __init__(
        self, partition: list[DuckColumn] | None = None, order: list[DuckColumn] | None = None
    ) -> None:
        self._partition = partition or []
        self._order = order or []

    def partitionBy(self, *cols: Any) -> DuckWindowSpec:  # noqa: N802
        """Set the partition keys."""
        return DuckWindowSpec(columns_from_args(cols), self._order)

    def orderBy(self, *cols: Any) -> DuckWindowSpec:  # noqa: N802
        """Set the ordering."""
        return DuckWindowSpec(self._partition, columns_from_args(cols))

    def sql(self, ctx: RenderCtx) -> str:
        """Render the ``OVER (...)`` body."""
        parts: list[str] = []
        if self._partition:
            parts.append("PARTITION BY " + ", ".join(c.sql(ctx) for c in self._partition))
        if self._order:
            parts.append("ORDER BY " + ", ".join(c.order_sql(ctx) for c in self._order))
        return " ".join(parts)


class DuckWindow:
    """``Window`` entry point (``DuckWindow.partitionBy(...)``)."""

    @staticmethod
    def partitionBy(*cols: Any) -> DuckWindowSpec:  # noqa: N802
        """Start a window spec with partition keys."""
        return DuckWindowSpec().partitionBy(*cols)

    @staticmethod
    def orderBy(*cols: Any) -> DuckWindowSpec:  # noqa: N802
        """Start a window spec with an ordering."""
        return DuckWindowSpec().orderBy(*cols)
