"""DuckDB engine x storage matrix against local docker emulators.

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
disc -> <store> -> disc through the real
:class:`~ovid2.core.engines.duckdb_stager.DuckDBEngineStager` and asserts
the content (row multiset + column set) is unchanged, plus the
exact-location layout (``{prefix}/{table}.csv`` on object stores).
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
    yield from _container("adobe/s3mock:latest", port, 9090, env={"initialBuckets": "ovid2"})


@pytest.fixture(scope="session")
def azurite_port() -> Iterator[int]:
    port = _free_port()
    yield from _container(
        "mcr.microsoft.com/azure-storage/azurite:latest",
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
        "fsouza/fake-gcs-server:latest",
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
    gen = _container("postgres:16", port, 5432, env={"POSTGRES_PASSWORD": "pw"})
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
        "mcr.microsoft.com/mssql/server:2022-latest",
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


@pytest.fixture()
def engine() -> Iterator[Any]:
    from ovid2.core.engines import dsl
    from ovid2.core.engines.duckdb import DuckDBEngine

    eng = DuckDBEngine(args={"threads": 4, "memory_limit": "4GB"})
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
