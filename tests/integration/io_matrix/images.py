"""Pinned emulator images for the DuckDB I/O matrix (single source of truth).

Pinned by digest so a moved ``:latest`` tag can neither break nor silently
change the matrix. ``scripts/ci_checks.sh io_matrix`` pulls exactly these:
``python -m tests.integration.io_matrix.images`` prints one per line. Bump
deliberately — pull the new tag and copy its ``RepoDigests`` entry here.
"""

from __future__ import annotations

EMULATOR_IMAGES: dict[str, str] = {
    "s3": "adobe/s3mock@sha256:ab01a6946750f451ca215a47e91030695b260e4003b8a5a6201d25029b8fca92",
    "azure_blob": (
        "mcr.microsoft.com/azure-storage/azurite"
        "@sha256:830430c1da1a2d537e08f3e6764dd1f5ae00cf0346bcaf625b968ec3f0971fd5"
    ),
    "gcs": (
        "fsouza/fake-gcs-server"
        "@sha256:797ce226d62f947c009dc40246b30cfb456b8473d8241407f9d6f2c04e4d69ef"
    ),
    "postgres": "postgres@sha256:1beaa3084eee2676f20a9cd9928f0bf2c5bf674f8e49f81017912183d377c61c",
    "mssql": (
        "mcr.microsoft.com/mssql/server"
        "@sha256:cf8f0ab2ebe61feec2eed2629061a07d247f791c2a8ddda74a4ac9f06a291718"
    ),
}


if __name__ == "__main__":
    print("\n".join(EMULATOR_IMAGES.values()))
