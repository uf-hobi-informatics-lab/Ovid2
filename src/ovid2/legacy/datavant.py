"""Legacy-Ovid Datavant ``transform-tokens`` step (``legacy_ovid`` profile).

Port of ``CommonFuncitons.datavant_transfer_tokens_to_pcornet`` and
``…_parallel``: runs the Datavant CLI with exactly the legacy arguments::

    <cred_dir>/Datavant_Linux_ubuntu2004_x64 transform-tokens \\
        --to pcornet --from <DATAVANT_KEY> -s <site> -i <file> -o <work>

with ``<cred_dir>/dv_credentials.txt`` on stdin, chunking files larger than
``HASH_TOKEN_CHUNK_SIZE_TARGET`` into at most ``HASH_TOKEN_MAX_CHUNKS``
parallel runs and concatenating the ``*_out`` files (header once).

The *ancillary* legacy ``datavant_export_to_datavant_inbox`` (a side copy
for downstream match-ID generation that never affects ETL output) is not
part of the parity scope and is a logged no-op here.

The credential directory is :data:`ovid2.config.settings.LEGACY_CREDENTIALS_DIR`
(``$OVID2_CREDENTIALS_DIR``) — the executor-tier credential mount.
"""

from __future__ import annotations

import glob
import logging
import math
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logger = logging.getLogger(__name__)

#: Legacy ``settings.HASH_TOKEN_CHUNK_SIZE_TARGET`` / ``HASH_TOKEN_MAX_CHUNKS``.
CHUNK_SIZE_TARGET: int = 50 * 1024 * 1024
MAX_CHUNKS: int = 16
#: Legacy ``ovid_secrets`` file names inside the credential directory.
DV_CREDENTIALS_FILE_NAME: str = os.environ.get("OVID2_DV_CREDENTIALS_FILE", "dv_credentials.txt")
DV_TRANSFORMATION_SCRIPT_FILE_NAME: str = os.environ.get(
    "OVID2_DV_BINARY", "Datavant_Linux_ubuntu2004_x64"
)
#: Legacy ``-s`` site argument.
DV_SITE: str = os.environ.get("OVID2_DV_SITE", "")


class DatavantError(RuntimeError):
    """Raised when the Datavant CLI fails."""


def _cred_dir() -> Path:
    from ovid2.config.settings import LEGACY_CREDENTIALS_DIR

    return Path(LEGACY_CREDENTIALS_DIR)


def _run_cli(input_file: str, output_dir: str, datavant_key: str) -> tuple[int, bytes, bytes]:
    os.makedirs(output_dir, exist_ok=True)
    cred = _cred_dir()
    with (cred / DV_CREDENTIALS_FILE_NAME).open("rb") as cred_file:
        process = subprocess.Popen(
            [
                str(cred / DV_TRANSFORMATION_SCRIPT_FILE_NAME),
                "transform-tokens",
                "--to",
                "pcornet",
                "--from",
                datavant_key,
                "-s",
                DV_SITE,
                "-i",
                input_file,
                "-o",
                output_dir,
            ],
            stdin=cred_file,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=output_dir,
        )
        stdout, stderr = process.communicate()
    if stdout:
        logger.info("[DATAVANT stdout] %s", stdout.decode(errors="replace"))
    if stderr:
        logger.warning("[DATAVANT stderr] %s", stderr.decode(errors="replace"))
    return process.returncode, stdout, stderr


def transfer_tokens_to_pcornet(
    input_file_path: str, file_name: str, working_dir: str, datavant_key: str
) -> None:
    """Legacy ``datavant_transfer_tokens_to_pcornet`` (sequential).

    Every file matching ``input_file_path + file_name`` is transformed into
    ``working_dir`` (recreated). Legacy ignored the CLI exit code here.
    """
    if os.path.exists(working_dir):
        shutil.rmtree(working_dir)
    os.makedirs(working_dir)
    for match in glob.glob(input_file_path + file_name, recursive=True):
        _run_cli(input_file_path + os.path.basename(match), working_dir, datavant_key)


def transfer_tokens_to_pcornet_parallel(
    input_file_path: str, file_name: str, working_dir: str, datavant_key: str
) -> None:
    """Legacy ``datavant_transfer_tokens_to_pcornet_parallel``."""
    if os.path.exists(working_dir):
        shutil.rmtree(working_dir)
    os.makedirs(working_dir)

    candidate = os.path.join(input_file_path, file_name)
    if os.path.isfile(candidate):
        full_input_file = candidate
    else:
        matches = [m for m in sorted(glob.glob(candidate, recursive=True)) if os.path.isfile(m)]
        if not matches:
            raise FileNotFoundError(f"Input file not found: {candidate}")
        if len(matches) > 1:
            logger.warning(
                "Datavant glob %r matched %d files; using %s", file_name, len(matches), matches[0]
            )
        full_input_file = matches[0]
    file_name = os.path.basename(full_input_file)

    num_chunks = min(
        max(1, math.ceil(os.path.getsize(full_input_file) / CHUNK_SIZE_TARGET)), MAX_CHUNKS
    )
    if num_chunks == 1:
        transfer_tokens_to_pcornet(input_file_path, file_name, working_dir, datavant_key)
        return

    base_name, ext = os.path.splitext(file_name)
    with open(full_input_file, "rb") as fh:
        total_rows = sum(1 for _ in fh) - 1
    if total_rows <= 0:
        raise ValueError(f"Input file has no data rows: {full_input_file}")
    rows_per_chunk = math.ceil(total_rows / num_chunks)

    chunks_dir = os.path.join(working_dir, "datavant_chunks")
    os.makedirs(chunks_dir, exist_ok=True)
    chunk_paths: list[str] = []
    with open(full_input_file, "rb") as src:
        header = src.readline()
        out = None
        for i, line in enumerate(src):
            idx = min(i // rows_per_chunk, num_chunks - 1)
            path = os.path.join(chunks_dir, f"{base_name}_chunk_{idx + 1:03d}{ext}")
            if not chunk_paths or chunk_paths[-1] != path:
                if out is not None:
                    out.close()
                out = open(path, "wb")  # noqa: SIM115 — closed on the next switch / below
                out.write(header)
                chunk_paths.append(path)
            assert out is not None
            out.write(line)
        if out is not None:
            out.close()

    out_dirs = [os.path.join(working_dir, f"chunk_{i + 1:03d}") for i in range(len(chunk_paths))]
    results: list[tuple[int, bytes, bytes] | None] = [None] * len(chunk_paths)
    with ThreadPoolExecutor(max_workers=len(chunk_paths)) as pool:
        futures = {
            pool.submit(_run_cli, chunk_paths[i], out_dirs[i], datavant_key): i
            for i in range(len(chunk_paths))
        }
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()
    for i, res in enumerate(results):
        if res is None or res[0] != 0:
            raise DatavantError(f"Datavant process failed for chunk in {out_dirs[i]}")

    combined = os.path.join(working_dir, f"{base_name}_out{ext}")
    with open(combined, "wb") as out_f:
        header_written = False
        for i, out_dir in enumerate(out_dirs):
            chunk_base = os.path.splitext(os.path.basename(chunk_paths[i]))[0]
            chunk_out = os.path.join(out_dir, f"{chunk_base}_out{ext}")
            if not os.path.isfile(chunk_out):
                raise FileNotFoundError(f"Expected Datavant output file not found: {chunk_out}")
            with open(chunk_out, "rb") as cf:
                first = cf.readline()
                if not header_written:
                    out_f.write(first)
                    header_written = True
                shutil.copyfileobj(cf, out_f)
    shutil.rmtree(chunks_dir, ignore_errors=True)
    for out_dir in out_dirs:
        shutil.rmtree(out_dir, ignore_errors=True)
