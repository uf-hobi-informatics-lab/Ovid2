"""Engine-portable data types (``pyspark.sql.types`` stand-ins).

Shared and generated pipeline code declares column types —
``udf(fn, StringType())``, ``col.cast(FloatType())``,
``createDataFrame(rows, StructType([...]))``. Importing them from here
instead of ``pyspark.sql.types`` keeps that code engine-portable:

* Every type **is a** :class:`str` holding its Spark DDL text
  (``"string"``, ``"float"``, ``"decimal(10,2)"``,
  ``"`a` string, `b` float"``). PySpark accepts a DDL string wherever it
  accepts a ``DataType`` (``Column.cast``, ``udf``, ``pandas_udf``,
  ``createDataFrame(schema=…)``), so a Spark plan built with these types
  is identical to one built with the real classes.
* Each type also exposes the parts of the ``pyspark.sql.types`` API that
  Ovid2 and the legacy ports read (``simpleString``, ``typeName``,
  ``StructType.fields`` / ``names`` / ``add``, ``StructField.name`` /
  ``dataType`` / ``nullable``), so the DuckDB and Snowpark engines map
  them to their own types without importing pyspark.

This module never imports pyspark.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

__all__ = [
    "ArrayType",
    "BinaryType",
    "BooleanType",
    "ByteType",
    "DataType",
    "DateType",
    "DecimalType",
    "DoubleType",
    "FloatType",
    "IntegerType",
    "LongType",
    "MapType",
    "NullType",
    "Row",
    "ShortType",
    "StringType",
    "StructField",
    "StructType",
    "TimestampNTZType",
    "TimestampType",
    "spark_type_name",
]


class DataType(str):
    """Base portable type: a ``str`` holding the Spark DDL type text."""

    _ddl: str = ""

    def __new__(cls, *_args: Any, **_kwargs: Any) -> DataType:
        return super().__new__(cls, cls._ddl)

    def simpleString(self) -> str:  # noqa: N802 — PySpark API
        """Return the Spark simple string (``"string"``, ``"decimal(10,2)"``)."""
        return str.__str__(self)

    def typeName(self) -> str:  # noqa: N802 — PySpark API
        """Return the Spark type name (``"string"``, ``"decimal"``)."""
        return self.simpleString().split("(", 1)[0]

    def jsonValue(self) -> str:  # noqa: N802 — PySpark API
        """Return the JSON form (the simple string for atomic types)."""
        return self.simpleString()

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.simpleString() == spark_type_name(other)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.simpleString())


def _atomic(name: str, ddl: str) -> type[DataType]:
    cls = type(name, (DataType,), {"_ddl": ddl, "__doc__": f"Portable Spark ``{ddl}`` type."})
    return cls


StringType = _atomic("StringType", "string")
BooleanType = _atomic("BooleanType", "boolean")
ByteType = _atomic("ByteType", "tinyint")
ShortType = _atomic("ShortType", "smallint")
IntegerType = _atomic("IntegerType", "int")
LongType = _atomic("LongType", "bigint")
FloatType = _atomic("FloatType", "float")
DoubleType = _atomic("DoubleType", "double")
DateType = _atomic("DateType", "date")
TimestampType = _atomic("TimestampType", "timestamp")
TimestampNTZType = _atomic("TimestampNTZType", "timestamp_ntz")
BinaryType = _atomic("BinaryType", "binary")
NullType = _atomic("NullType", "void")


class DecimalType(DataType):
    """Portable ``decimal(precision, scale)``."""

    def __new__(cls, precision: int = 10, scale: int = 0) -> DecimalType:
        obj = str.__new__(cls, f"decimal({int(precision)},{int(scale)})")
        obj.precision = int(precision)  # type: ignore[attr-defined]
        obj.scale = int(scale)  # type: ignore[attr-defined]
        return obj

    def __repr__(self) -> str:
        return f"DecimalType({self.precision}, {self.scale})"  # type: ignore[attr-defined]


class ArrayType(DataType):
    """Portable ``array<element>``."""

    def __new__(cls, elementType: Any, containsNull: bool = True) -> ArrayType:  # noqa: N803
        element = _as_type(elementType)
        obj = str.__new__(cls, f"array<{element.simpleString()}>")
        obj.elementType = element  # type: ignore[attr-defined]
        obj.containsNull = containsNull  # type: ignore[attr-defined]
        return obj


class MapType(DataType):
    """Portable ``map<key, value>``."""

    def __new__(cls, keyType: Any, valueType: Any, valueContainsNull: bool = True) -> MapType:  # noqa: N803
        key, value = _as_type(keyType), _as_type(valueType)
        obj = str.__new__(cls, f"map<{key.simpleString()},{value.simpleString()}>")
        obj.keyType = key  # type: ignore[attr-defined]
        obj.valueType = value  # type: ignore[attr-defined]
        obj.valueContainsNull = valueContainsNull  # type: ignore[attr-defined]
        return obj


class StructField:
    """Portable ``StructField(name, dataType, nullable)``."""

    def __init__(
        self,
        name: str,
        dataType: Any,  # noqa: N803
        nullable: bool = True,
        metadata: Any = None,
    ) -> None:
        self.name = str(name)
        self.dataType = _as_type(dataType)
        self.nullable = bool(nullable)
        self.metadata = dict(metadata or {})

    def ddl(self) -> str:
        """Return ``\\`name\\` type [NOT NULL]``."""
        quoted = "`" + self.name.replace("`", "``") + "`"
        return f"{quoted} {self.dataType.simpleString()}" + ("" if self.nullable else " NOT NULL")

    def simpleString(self) -> str:  # noqa: N802 — PySpark API
        """Return ``name:type``."""
        return f"{self.name}:{self.dataType.simpleString()}"

    def __repr__(self) -> str:
        return f"StructField({self.name!r}, {self.dataType!r}, {self.nullable})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, StructField):
            return NotImplemented
        return (self.name, self.dataType, self.nullable) == (
            other.name,
            other.dataType,
            other.nullable,
        )

    def __hash__(self) -> int:
        return hash((self.name, self.dataType, self.nullable))


class StructType(DataType):
    """Portable ``StructType([StructField, ...])`` — a DDL schema string."""

    fields: list[StructField]

    def __new__(cls, fields: Iterable[StructField] | None = None) -> StructType:
        items = list(fields or [])
        obj = str.__new__(cls, ", ".join(f.ddl() for f in items))
        obj.fields = items
        return obj

    @property
    def names(self) -> list[str]:
        """Field names in order."""
        return [f.name for f in self.fields]

    def fieldNames(self) -> list[str]:  # noqa: N802 — PySpark API
        """Field names in order."""
        return self.names

    def add(
        self, field: Any, data_type: Any = None, nullable: bool = True, metadata: Any = None
    ) -> StructType:
        """Return a new struct with one more field (PySpark returns ``self`` mutated)."""
        new = (
            field
            if isinstance(field, StructField)
            else StructField(field, data_type, nullable, metadata)
        )
        return StructType([*self.fields, new])

    def simpleString(self) -> str:  # noqa: N802 — PySpark API
        """Return ``struct<a:string,b:float>``."""
        return "struct<" + ",".join(f.simpleString() for f in self.fields) + ">"

    def typeName(self) -> str:  # noqa: N802 — PySpark API
        """Return ``struct``."""
        return "struct"

    def __iter__(self) -> Iterator[StructField]:  # type: ignore[override]
        return iter(self.fields)

    def __len__(self) -> int:
        return len(self.fields)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self.fields[key]
        if isinstance(key, str):
            for f in self.fields:
                if f.name == key:
                    return f
            raise KeyError(key)
        return str(str.__getitem__(self, key))

    def __repr__(self) -> str:
        return f"StructType({self.fields!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, StructType):
            return self.fields == other.fields
        return NotImplemented

    def __hash__(self) -> int:
        return hash(tuple(self.fields))


class Row(tuple):  # type: ignore[type-arg]
    """Portable ``Row`` (``Row(a=1)`` / ``Row("a", "b")(1, 2)``)."""

    __fields__: tuple[str, ...] = ()

    def __new__(cls, *args: Any, **kwargs: Any) -> Row:
        if kwargs:
            row = super().__new__(cls, tuple(kwargs.values()))
            row.__fields__ = tuple(kwargs)
            return row
        return super().__new__(cls, args)

    def __call__(self, *values: Any) -> Row:
        row = tuple.__new__(Row, values)
        row.__fields__ = tuple(str(v) for v in self)
        return row

    def asDict(self, recursive: bool = False) -> dict[str, Any]:  # noqa: N802 — PySpark API
        """Return ``{field: value}``."""
        del recursive
        return dict(zip(self.__fields__, self, strict=False))

    def _field_index(self, name: str) -> int:
        """Exact field match, else a unique case-insensitive one.

        Snowflake folds unquoted identifiers to upper case, so a Snowpark
        row built from ``alias("max_date")`` carries ``MAX_DATE``; Spark
        code still asks for ``row["max_date"]``.
        """
        try:
            return self.__fields__.index(name)
        except ValueError:
            folded = [i for i, f in enumerate(self.__fields__) if f.lower() == name.lower()]
            if len(folded) == 1:
                return folded[0]
            raise

    def __getattr__(self, name: str) -> Any:
        try:
            return tuple.__getitem__(self, self._field_index(name))
        except ValueError as exc:
            raise AttributeError(name) from exc

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            return tuple.__getitem__(self, self._field_index(key))
        return tuple.__getitem__(self, key)


_ALIASES: dict[str, str] = {
    "str": "string",
    "varchar": "string",
    "text": "string",
    "integer": "int",
    "long": "bigint",
    "short": "smallint",
    "byte": "tinyint",
    "bool": "boolean",
    "real": "float",
    "null": "void",
}


def spark_type_name(data_type: Any) -> str:
    """Return the Spark simple string for a portable / PySpark type or DDL text."""
    simple = getattr(data_type, "simpleString", None)
    text = str(simple()) if callable(simple) else str(data_type)
    text = text.strip().lower()
    return _ALIASES.get(text, text)


def _as_type(value: Any) -> DataType:
    """Coerce a portable type, a PySpark type or DDL text to a portable type."""
    if isinstance(value, DataType):
        return value
    name = spark_type_name(value)
    for cls in (
        StringType,
        BooleanType,
        ByteType,
        ShortType,
        IntegerType,
        LongType,
        FloatType,
        DoubleType,
        DateType,
        TimestampType,
        TimestampNTZType,
        BinaryType,
        NullType,
    ):
        if cls._ddl == name:
            return cls()
    if name.startswith("decimal"):
        inner = name[len("decimal") :].strip("() ")
        parts = [p.strip() for p in inner.split(",") if p.strip()]
        return DecimalType(*(int(p) for p in parts)) if parts else DecimalType()
    obj = str.__new__(DataType, name)
    return obj


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Library module; import ovid2.core.engines.types.")
