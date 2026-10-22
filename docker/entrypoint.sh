#!/usr/bin/env bash
# Ovid2 container entrypoint.
#
# Forwards all arguments to the ``ovid2`` CLI. Kept as a shim so future
# environment bootstrap (secret loading, Snowflake auth) can be added without
# a rebuild of every layer above.

set -euo pipefail

# Belt-and-braces ``py4j`` path resolution. The Dockerfile bakes a
# hardcoded ``PYTHONPATH`` including ``py4j-0.10.9.5-src.zip`` (the version
# shipped with Spark 3.3), but if the base image bumps to a different py4j
# version the hardcoded path would silently disappear from PYTHONPATH.
# Re-expand the glob here so ``import pyspark`` keeps working across
# Bitnami/Spark refreshes without another image rebuild being blocked on
# a Dockerfile edit. Falls back cleanly if the glob does not match.
if [[ -d "/opt/bitnami/spark/python/lib" ]]; then
    shopt -s nullglob
    _py4j_zips=(/opt/bitnami/spark/python/lib/py4j-*-src.zip)
    shopt -u nullglob
    if (( ${#_py4j_zips[@]} > 0 )); then
        _py4j_zip="${_py4j_zips[0]}"
        case ":${PYTHONPATH:-}:" in
            *":${_py4j_zip}:"*) ;;  # already present, no-op
            *) export PYTHONPATH="${_py4j_zip}:${PYTHONPATH:-}" ;;
        esac
    fi
fi

exec ovid2 "$@"
