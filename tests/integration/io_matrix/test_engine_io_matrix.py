"""Engine x storage matrix against local docker emulators (every engine).

Opt-in (``-m io_matrix``). Each emulator is started by the session
fixtures below when its image is present locally and torn down after
the run; a missing image or docker daemon skips that leg:

=============  ==========================================
driver         emulator
=============  ==========================================
s3             ``adobe/s3mock``
azure_blob     ``mcr.microsoft.com/azure-storage/azurite``
gcs            ``fsouza/fake-gcs-server``
postgres       ``postgres:16``
mssql          ``mcr.microsoft.com/mssql/server:2022-latest``
=============  ==========================================

Every leg round-trips the same PCORnet-shaped table
disc -> <store> -> disc through each engine's real stager — DuckDB
(:class:`~ovid2.core.engines.duckdb_stager.DuckDBEngineStager`) and
PySpark (:class:`~ovid2.core.engines.stager.SparkEngineStager`: native
data plane or the Arrow bridge) — and asserts the content (row multiset
+ column set) is unchanged, plus the exact-location layout
(``{prefix}/{table}.csv`` on object stores). Snowpark legs need a live
account and are covered by ``-m snowpark_live``.
"""

from __future__ import annotations

import contextlib
import csv
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.integration.io_matrix.images import EMULATOR_IMAGES

pytestmark = [pytest.mark.io_matrix, pytest.mark.integration]

duckdb = pytest.importorskip("duckdb")

_HEADER = ["PATID", "ENCOUNTERID", "ADMIT_DATE", "DX", "RAW_DX", "NOTE"]
_ROWS = [
    ["P1", "E1", "2021-03-01", "E11.9", "e11.9", "plain"],
    ["P2", "E2", "2021-4-5", "I10", "", "has, comma"],
    ["P3", "", "bad-date", "Z00.00", "x", "unicode é ☃"],
    ["P4", "E4", "", "", "", ""],
]


def _docker_ok() -> bool:
    return (
        shutil.which("docker") is not None
        and subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0
    )


def _image_present(image: str) -> bool:
    return (
        subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True, check=False
        ).returncode
        == 0
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_port(port: int, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.5)
    raise TimeoutError(f"port {port} never opened")


def _container(
    image: str, port: int, inner: int, *extra: str, env: dict[str, str] | None = None
) -> Iterator[int]:
    if not _docker_ok() or not _image_present(image):
        pytest.skip(f"docker / image {image} unavailable")
    name = f"ovid2-iom-{uuid.uuid4().hex[:8]}"
    cmd = ["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:{inner}"]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [image, *extra]
    subprocess.run(cmd, check=True, capture_output=True)
    try:
        _wait_port(port)
        yield port
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


@pytest.fixture(scope="session")
def s3_port() -> Iterator[int]:
    port = _free_port()
    yield from _container(EMULATOR_IMAGES["s3"], port, 9090, env={"initialBuckets": "ovid2"})


@pytest.fixture(scope="session")
def azurite_port() -> Iterator[int]:
    port = _free_port()
    yield from _container(
        EMULATOR_IMAGES["azure_blob"],
        port,
        10000,
        "azurite-blob",
        "--blobHost",
        "0.0.0.0",
        "--skipApiVersionCheck",
    )


@pytest.fixture(scope="session")
def gcs_port() -> Iterator[int]:
    port = _free_port()
    yield from _container(
        EMULATOR_IMAGES["gcs"],
        port,
        port,
        "-scheme",
        "http",
        "-port",
        str(port),
        "-public-host",
        f"127.0.0.1:{port}",
    )


@pytest.fixture(scope="session")
def pg_port() -> Iterator[int]:
    port = _free_port()
    gen = _container(EMULATOR_IMAGES["postgres"], port, 5432, env={"POSTGRES_PASSWORD": "pw"})
    value = next(gen)
    # The port opens before initdb finishes; wait for real readiness.
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            import psycopg

            psycopg.connect(
                host="127.0.0.1", port=value, user="postgres", password="pw", dbname="postgres"
            ).close()
            break
        except Exception:
            time.sleep(1)
    yield value
    with contextlib.suppress(StopIteration):
        next(gen)


@pytest.fixture(scope="session")
def mssql_port() -> Iterator[int]:
    port = _free_port()
    gen = _container(
        EMULATOR_IMAGES["mssql"],
        port,
        1433,
        env={"ACCEPT_EULA": "Y", "MSSQL_SA_PASSWORD": "Ovid2_Test!pw"},
    )
    value = next(gen)
    pymssql = pytest.importorskip("pymssql")
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            pymssql.connect(
                server="127.0.0.1",
                port=value,
                user="sa",
                password="Ovid2_Test!pw",
                database="master",
                login_timeout=5,
            ).close()
            break
        except Exception:
            time.sleep(2)
    yield value
    with contextlib.suppress(StopIteration):
        next(gen)


# ---------------------------------------------------------------------------


@pytest.fixture(params=["duckdb", "pyspark"])
def engine(request: pytest.FixtureRequest) -> Iterator[Any]:
    from ovid2.core.engines import dsl

    if request.param == "duckdb":
        from ovid2.core.engines.duckdb import DuckDBEngine

        eng: Any = DuckDBEngine(args={"threads": 4, "memory_limit": "4GB"})
    else:
        pytest.importorskip("pyspark")
        from ovid2.core.engines.pyspark import PySparkEngine

        eng = PySparkEngine(args={"spark.master": "local[2]"})
    dsl.set_active_engine(eng)
    yield eng
    eng.stop_session()
    dsl.set_active_engine(None)


@pytest.fixture()
def disc_in(tmp_path: Path) -> Any:
    from ovid2.drivers.disc.driver import DiscDriver

    root = tmp_path / "in"
    root.mkdir()
    with (root / "DIAGNOSIS.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        writer.writerow(_HEADER)
        writer.writerows(_ROWS)
    return DiscDriver(args={"host_path": str(root)}, location={"format": "csv"})


def _disc_out(tmp_path: Path, name: str) -> Any:
    from ovid2.drivers.disc.driver import DiscDriver

    root = tmp_path / name
    return DiscDriver(args={"host_path": str(root)}, location={"format": "csv"}), root


def _rows(path: Path) -> tuple[list[str], list[tuple[str, ...]]]:
    with path.open(encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    header = lines[0].split("\t")
    return header, sorted(tuple(line.split("\t")) for line in lines[1:])


def _expected() -> list[tuple[str, ...]]:
    # Spark disc contract: '' (null) cells render empty, but an explicitly
    # empty string written by the source is NULL after the trim/NULLIF read.
    return sorted(tuple(c for c in r) for r in _ROWS)


def _roundtrip(
    engine: Any, disc_in: Any, store: Any, tmp_path: Path, table: str = "diagnosis"
) -> list[tuple[str, ...]]:
    stager = engine.get_stager()
    df = stager.materialise(disc_in, "p", "b", "DIAGNOSIS.csv", table_name=table)
    stager.persist(df, store, "p", "b", table)
    back = stager.materialise(
        store, "p", "b", f"{table}.csv" if _is_object_store(store) else table, table_name=table
    )
    out, root = _disc_out(tmp_path, f"out-{store.type_name}")
    stager.persist(back, out, "p", "b", table)
    header, rows = _rows(root / f"{table}.csv")
    assert header == _HEADER
    return rows


def _is_object_store(driver: Any) -> bool:
    return driver.type_name in ("s3", "azure_blob", "gcs")


def test_disc_to_disc(engine: Any, disc_in: Any, tmp_path: Path) -> None:
    stager = engine.get_stager()
    out, root = _disc_out(tmp_path, "out")
    stager.persist(
        stager.materialise(disc_in, "p", "b", "DIAGNOSIS.csv"), out, "p", "b", "diagnosis"
    )
    header, rows = _rows(root / "diagnosis.csv")
    assert header == _HEADER
    assert rows == _expected()


def test_s3(engine: Any, disc_in: Any, tmp_path: Path, s3_port: int) -> None:
    import boto3

    from ovid2.drivers.s3.driver import S3Driver

    s3 = boto3.client(
        "s3",
        endpoint_url=f"http://127.0.0.1:{s3_port}",
        region_name="us-east-1",
        aws_access_key_id="x",
        aws_secret_access_key="y",
    )
    with contextlib.suppress(Exception):  # already exists
        s3.create_bucket(Bucket="ovid2")
    store = S3Driver(
        args={"region": "us-east-1", "endpoint_url": f"http://127.0.0.1:{s3_port}"},
        secrets={"aws_access_key_id": "x", "aws_secret_access_key": "y"},
        location={"bucket": "ovid2", "path": "landing/run1", "format": "csv", "single_file": True},
    )
    assert _roundtrip(engine, disc_in, store, tmp_path) == _expected()
    keys = [
        o["Key"]
        for o in s3.list_objects_v2(Bucket="ovid2", Prefix="landing/run1/").get("Contents", [])
    ]
    assert keys == ["landing/run1/diagnosis.csv"]  # exact location, single object


def test_azure_blob(engine: Any, disc_in: Any, tmp_path: Path, azurite_port: int) -> None:
    from azure.storage.blob import BlobServiceClient

    from ovid2.drivers.azure_blob.driver import AzureBlobDriver

    conn_str = (
        "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
        "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
        f"BlobEndpoint=http://127.0.0.1:{azurite_port}/devstoreaccount1;"
    )
    service = BlobServiceClient.from_connection_string(conn_str)
    with contextlib.suppress(Exception):  # already exists
        service.create_container("ovid2")
    store = AzureBlobDriver(
        args={"account_name": "devstoreaccount1"},
        secrets={"connection_string": conn_str},
        location={
            "container": "ovid2",
            "path": "landing/run1",
            "format": "csv",
            "single_file": True,
        },
    )
    assert _roundtrip(engine, disc_in, store, tmp_path) == _expected()
    names = [
        b.name
        for b in service.get_container_client("ovid2").list_blobs(name_starts_with="landing/run1/")
    ]
    assert names == ["landing/run1/diagnosis.csv"]


def test_gcs(
    engine: Any, disc_in: Any, tmp_path: Path, gcs_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import storage

    from ovid2.drivers.registry import instantiate

    endpoint = f"http://127.0.0.1:{gcs_port}"
    monkeypatch.setenv("OVID2_GCS_ENDPOINT", endpoint)
    client = storage.Client(
        project="ovid2",
        credentials=AnonymousCredentials(),
        client_options={"api_endpoint": endpoint},
    )
    with contextlib.suppress(Exception):  # already exists
        client.create_bucket("ovid2")
    store = instantiate(
        "gcs",
        args={"project_id": "ovid2"},
        secrets={},
        location={"bucket": "ovid2", "path": "landing/run1", "format": "csv", "single_file": True},
    )
    assert _roundtrip(engine, disc_in, store, tmp_path) == _expected()
    names = [b.name for b in client.list_blobs("ovid2", prefix="landing/run1/")]
    assert names == ["landing/run1/diagnosis.csv"]


def test_postgres(engine: Any, disc_in: Any, tmp_path: Path, pg_port: int) -> None:
    from ovid2.drivers.registry import instantiate

    store = instantiate(
        "postgres",
        args={"host": "127.0.0.1", "port": pg_port, "database": "postgres", "user": "postgres"},
        secrets={"password": "pw"},
        location={"database": "postgres", "schema": "ovid2_iom"},
    )
    assert _roundtrip(engine, disc_in, store, tmp_path) == _expected()


def test_mssql(engine: Any, disc_in: Any, tmp_path: Path, mssql_port: int) -> None:
    from ovid2.drivers.registry import instantiate

    store = instantiate(
        "mssql",
        args={"host": "127.0.0.1", "port": mssql_port, "database": "master", "user": "sa"},
        secrets={"password": "Ovid2_Test!pw"},
        location={"database": "master", "schema": "ovid2_iom"},
    )
    assert _roundtrip(engine, disc_in, store, tmp_path, table="DIAGNOSIS") == _expected()


# ---------------------------------------------------------------------------
# Engine x driver coverage: every pair runs a pipeline, or is listed refused
# ---------------------------------------------------------------------------

#: Pairs this matrix does NOT run, with the reason. Everything else in
#: ENGINE_TYPE_MAP x DRIVERS must have a leg here (``_LEG_TESTS``) — a new
#: engine or driver fails ``test_every_engine_driver_pair_is_covered`` until
#: it gets a leg or a documented reason.
REFUSED_PAIRS: dict[tuple[str, str], str] = {
    **{
        (engine, "access"): (
            "read-only driver (Driver.read_only): refused as an output by every engine at "
            "plan time; reads are covered by tests/integration/test_access_driver.py"
        )
        for engine in ("pyspark", "duckdb", "snowpark", "databricks_spark")
    },
    **{
        (engine, driver): (
            "no local emulator: needs a live account (snowflake / databricks are SaaS); "
            "Snowpark x snowflake is covered by the conformance suite with "
            "OVID2_TEST_SNOWPARK_ENGINE"
        )
        for engine in ("pyspark", "duckdb", "snowpark", "databricks_spark")
        for driver in ("snowflake", "databricks")
    },
    **{
        ("snowpark", driver): (
            "remote engine: needs a live Snowflake account to stage through "
            "(run with OVID2_TEST_SNOWPARK_ENGINE; the emulators are not reachable "
            "from Snowflake compute)"
        )
        for driver in ("disc", "s3", "azure_blob", "gcs", "postgres", "mssql")
    },
    **{
        ("databricks_spark", driver): (
            "remote engine: needs a live Databricks workspace (Databricks Connect); "
            "no emulator exists"
        )
        for driver in ("disc", "s3", "azure_blob", "gcs", "postgres", "mssql")
    },
}

#: Engines run locally by this matrix (the ``engine`` fixture params).
_LOCAL_ENGINES: tuple[str, ...] = ("duckdb", "pyspark")

#: Driver -> leg test name (each leg is parametrised over ``_LOCAL_ENGINES``).
_LEG_TESTS: dict[str, str] = {
    "disc": "test_disc_to_disc",
    "s3": "test_s3",
    "azure_blob": "test_azure_blob",
    "gcs": "test_gcs",
    "postgres": "test_postgres",
    "mssql": "test_mssql",
}


def test_every_engine_driver_pair_is_covered() -> None:
    """Each engine x driver pair either has a leg or a documented refusal."""
    from ovid2.core.engines.registry import ENGINE_TYPE_MAP
    from ovid2.drivers.registry import DRIVERS

    missing = []
    for engine_type in ENGINE_TYPE_MAP:
        for driver_type in DRIVERS:
            pair = (engine_type, driver_type)
            if pair in REFUSED_PAIRS:
                continue
            if engine_type in _LOCAL_ENGINES and driver_type in _LEG_TESTS:
                assert _LEG_TESTS[driver_type] in globals(), pair
                continue
            missing.append(pair)
    assert not missing, f"engine x driver pairs with neither a leg nor a reason: {missing}"
    stale = [p for p in REFUSED_PAIRS if p[0] not in ENGINE_TYPE_MAP or p[1] not in DRIVERS]
    assert not stale, f"REFUSED_PAIRS names unknown engines / drivers: {stale}"


def test_refused_output_pairs_are_refused_at_plan_time(tmp_path: Path) -> None:
    """Every engine refuses the read-only driver as an output before running."""
    from ovid2.drivers.engine_compat import _assert_stager_supports_drivers
    from ovid2.drivers.registry import DRIVERS, load_class

    del tmp_path
    read_only = [t for t in DRIVERS if bool(getattr(load_class(t), "read_only", False))]
    assert read_only == ["access"]
    for engine_type in ("pyspark", "duckdb", "snowpark", "databricks_spark"):
        assert (engine_type, "access") in REFUSED_PAIRS
    del _assert_stager_supports_drivers


def _write_input(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with (root / "Demographic_1.txt").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter="|", lineterminator="\n")
        writer.writerow(["PATID", "SEX", "BIRTH_DATE"])
        writer.writerows([["P1", "f", "2000-01-01"], ["P1", "f", "2000-01-01"], ["P2", "M", ""]])


def test_pipeline_end_to_end_disc(engine: Any, tmp_path: Path, monkeypatch: Any) -> None:
    """One small pipeline (format -> dedup -> transform -> write) through ``run_pipeline``.

    Exercises the single pipeline path (``core/pipeline._run_pipeline``) on
    each local engine over the disc driver: the formatter spec, the
    composite-key deduplicator and an assigned transform chain, then the
    stager write.
    """
    import sys

    from ovid2.config import addresses as addresses_mod
    from ovid2.core.loaders import load_formatter, load_transformations
    from ovid2.core.pipeline import run_pipeline
    from ovid2.drivers.disc import DiscAddress

    pkg_root = tmp_path / "pkgs"
    pkg = pkg_root / "ovid2_pipelines" / "iom_e2e"
    (pkg / "formatters").mkdir(parents=True)
    (pkg / "transforms").mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "formatters" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "pipeline_settings.py").write_text('cdm = "pcornet_7_0"\n', encoding="utf-8")
    (pkg / "formatters" / "demographic.yaml").write_text(
        "v: 1\nformatter: demographic\nadapter: source_passthrough\n"
        "input_file_name: '*Demographic_*.txt'\n"
        "sources:\n  demographic:\n    glob: '*Demographic_*.txt'\n"
        "projections:\n  PATID: patid\n  SEX: sex\n  BIRTH_DATE: birth_date\n",
        encoding="utf-8",
    )
    (pkg / "transforms" / "assignments.yaml").write_text(
        "v: 1\ntables:\n  demographic:\n    - steps:\n        - {op: to_upper, columns: [SEX]}\n",
        encoding="utf-8",
    )
    import ovid2_pipelines

    monkeypatch.setattr(
        ovid2_pipelines, "__path__", [*ovid2_pipelines.__path__, str(pkg_root / "ovid2_pipelines")]
    )
    monkeypatch.syspath_prepend(str(pkg_root))

    in_root, out_root = tmp_path / "in", tmp_path / "out"
    _write_input(in_root)
    out_root.mkdir()
    configs = tmp_path / "configs"
    configs.mkdir()
    for name, root in (("iom_in", in_root), ("iom_out", out_root)):
        (configs / f"{name}.json").write_text(
            f'{{"src_name": "disc", "src_args": {{"path": "{root}"}}}}',
            encoding="utf-8",
        )
    monkeypatch.setenv("OVID2_CONFIGS_DIR", str(configs))
    del addresses_mod

    input_address = DiscAddress({"path": str(in_root)})
    output_address = DiscAddress({"path": str(out_root)})
    session = engine.get_session()
    formatter = load_formatter(
        "iom_e2e",
        "demographic",
        spark=session,
        input_address=input_address,
        batch_id="b",
        engine=engine,
        input_address_name="iom_in",
    )
    try:
        run_pipeline(
            engine=engine,
            input_address=input_address,
            output_address=output_address,
            table_name="demographic",
            formatter=formatter,
            transformations=load_transformations(["format", "deduplicate", "transform"]),
            pipeline="iom_e2e",
            batch_id="b",
            input_address_name="iom_in",
            output_address_name="iom_out",
        )
    finally:
        for mod in [m for m in sys.modules if m.startswith("ovid2_pipelines.iom_e2e")]:
            sys.modules.pop(mod, None)
    header, rows = _rows(out_root / "demographic.csv")
    assert header == ["PATID", "SEX", "BIRTH_DATE"]
    # Duplicate P1 removed by the composite-key dedup; SEX upper-cased by the
    # assigned transform; the empty BIRTH_DATE stays empty.
    assert rows == [("P1", "F", "2000-01-01"), ("P2", "M", "")], rows
