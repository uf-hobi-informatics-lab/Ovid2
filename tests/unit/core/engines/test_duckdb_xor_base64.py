"""DuckDB native id encryption is byte-identical to the Python UDF.

``ovid2.utils.common.encrypt_id_udf`` (legacy XOR + base64 id encryption)
ran as a pandas UDF on DuckDB. Python UDFs hold the GIL, so on a 96-core
host a legacy ``acme`` run spent ~75 % of its samples in that one function
on one core. DuckDB now computes the same bytes natively
(``_DuckFunctions.xor_base64``); these tests pin byte parity with the
Python reference (unicode, empty, long, key-length boundaries, NULL) and
the fallback to the UDF for non-string inputs.
"""

from __future__ import annotations

import random
from base64 import b64encode
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("duckdb")

from ovid2.core.engines import dsl
from ovid2.core.engines.duckdb import DuckDBEngine

pytestmark = pytest.mark.unit

_KEY = bytes(range(7, 27))  # 20 bytes (a SHA-1 digest is 20)


def _ref(value: Any, prefix: str, key: bytes) -> str | None:
    if value is None:
        return None
    p = f"{prefix}{value}".encode()
    return b64encode(bytes(p[i] ^ key[i % len(key)] for i in range(len(p)))).decode()


@pytest.fixture()
def engine() -> Iterator[DuckDBEngine]:
    eng = DuckDBEngine(args={"threads": 4, "memory_limit": "1GB"})
    dsl.set_active_engine(eng)
    yield eng
    eng.stop_session()
    dsl.set_active_engine(None)


def _values() -> list[str]:
    rnd = random.Random(7)
    vals = [str(rnd.randint(0, 10**30)) for _ in range(3000)]
    vals += ["", " ", "\t", "héllo✓", "日本語", "x" * 500, "y" * 17, "z" * 20, "w" * 40, "a'b"]
    return vals


def _frame(engine: DuckDBEngine, values: list[str | None]) -> Any:
    conn = engine.connection()
    conn.execute("CREATE OR REPLACE TABLE __xor_in (id VARCHAR, n BIGINT)")
    conn.executemany("INSERT INTO __xor_in VALUES (?, ?)", [[v, i] for i, v in enumerate(values)])
    return engine.wrap(conn.sql("SELECT * FROM __xor_in"))


@pytest.mark.parametrize("prefix", ["ACME", "None", "SITE"])
@pytest.mark.parametrize("key", [_KEY, b"\x01", bytes(range(32))])
def test_native_matches_python(engine: DuckDBEngine, prefix: str, key: bytes) -> None:
    fns = engine.get_functions()
    values: list[str | None] = [*_values(), None]
    rows = _frame(engine, values).select(
        "id", fns.xor_base64(fns.col("id"), prefix, key).alias("e")
    )
    got = {r[0]: r[1] for r in rows.collect()}
    for v in values:
        assert got[v] == _ref(v, prefix, key), v


def test_encrypt_id_udf_uses_native_sql_and_matches(
    engine: DuckDBEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ovid2.utils import common

    monkeypatch.setattr(common, "_get_encryption_key", lambda: _KEY)
    udf = common.encrypt_id_udf("acme")
    values: list[str | None] = [*_values()[:500], None]
    frame = _frame(engine, values)
    col = udf(dsl.col("id"))
    sql = col.sql(frame._ctx()) if hasattr(frame, "_ctx") else ""
    assert "__ovid2_udf_" not in sql  # no Python UDF in the plan
    assert _KEY.hex() not in sql  # the key is never spliced into SQL text
    got = {r[0]: r[1] for r in frame.select("id", col.alias("e")).collect()}
    prefix = common.encryption_prefix("acme")
    for v in values:
        assert got[v] == _ref(v, prefix, _KEY)


def test_non_string_input_falls_back_to_the_udf(
    engine: DuckDBEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python renders ``f"{prefix}{1.0}"`` differently from a SQL cast — keep the UDF."""
    from ovid2.utils import common

    monkeypatch.setattr(common, "_get_encryption_key", lambda: _KEY)
    udf = common.encrypt_id_udf("acme")
    frame = engine.wrap(engine.connection().sql("SELECT (i * 1.5)::DOUBLE AS d FROM range(5) r(i)"))
    got = [r[0] for r in frame.select(udf(dsl.col("d")).alias("e")).collect()]
    prefix = common.encryption_prefix("acme")
    assert got == [_ref(i * 1.5, prefix, _KEY) for i in range(5)]


def test_rejects_empty_prefix_or_key(engine: DuckDBEngine) -> None:
    fns = engine.get_functions()
    with pytest.raises(ValueError):
        fns.xor_base64(fns.col("id"), "", _KEY)
    with pytest.raises(ValueError):
        fns.xor_base64(fns.col("id"), "ACME", b"")
