#!/usr/bin/env bash
# Ovid2 container entrypoint.
#
# Forwards all arguments to the ``ovid2`` CLI. Kept as a shim so future
# environment bootstrap (secret loading, Snowflake auth) can be added without
# a rebuild of every layer above.
#
# PySpark is the pip distribution (``$SPARK_HOME`` -> site-packages/pyspark),
# which brings its own ``py4j`` dependency, so no ``PYTHONPATH`` surgery is
# needed (the Bitnami-era py4j zip juggling is gone).

set -euo pipefail

exec ovid2 "$@"
