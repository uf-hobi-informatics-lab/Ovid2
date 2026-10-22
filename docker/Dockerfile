# Ovid2 runtime image.
#
# Multi-stage build:
#   * ``dialect`` — compiles the UCanAccess Spark JdbcDialect (JDK stage).
#   * ``base``    — Bitnami Spark + Python + system deps + Snowflake & UCanAccess JARs + dialect jar + XOR shim.
#   * ``final``   — installs the ``ovid2`` wheel built by hatch.
#
# Build (from repo root):
#   docker build -f docker/Dockerfile -t ovid2:${VERSION} .
#
# The ``XOR.py`` file must live alongside this Dockerfile.

# --------------------------------------------------------------------------
# Dialect stage: compile the UCanAccess Spark JdbcDialect.
#
# The Bitnami Spark image ships a JRE (no ``javac``), so compile the tiny
# dialect in a throwaway JDK stage against this exact Spark's jars (copied
# from the same Bitnami image), then COPY the resulting jar into ``base``.
# Pinned to the same Spark (3.3 / Scala 2.12) the runtime uses so the
# compiled ``JdbcDialect`` subclass is binary-compatible.
# --------------------------------------------------------------------------
FROM bitnamilegacy/spark:3.3 AS spark_jars_src

FROM eclipse-temurin:17-jdk AS dialect
COPY --from=spark_jars_src /opt/bitnami/spark/jars /spark-jars
COPY docker/ucanaccess-dialect/UcanaccessDialect.java /src/UcanaccessDialect.java
RUN mkdir -p /classes /out \
    && javac -cp "/spark-jars/*" -d /classes /src/UcanaccessDialect.java \
    && jar cf /out/ucanaccess-spark-dialect.jar -C /classes . \
    && ls -l /out/ucanaccess-spark-dialect.jar

# --------------------------------------------------------------------------
# Base runtime stage.
# --------------------------------------------------------------------------
FROM bitnamilegacy/spark:3.3 AS base

ARG SPARK_SNOWFLAKE_VERSION=2.16.0-spark_3.3
ARG SNOWFLAKE_JDBC_VERSION=3.14.2
# UCanAccess is the pure-JVM JDBC driver for Microsoft Access
# (.mdb / .accdb). It has three runtime jar dependencies — jackcess
# (the Access file reader), hsqldb (the in-memory relational engine
# UCanAccess maps the Access tables onto), and commons-lang3 (used by
# jackcess). commons-logging ships with the Bitnami base image.
ARG UCANACCESS_VERSION=5.0.1
ARG JACKCESS_VERSION=4.0.5
ARG HSQLDB_VERSION=2.5.0
ARG COMMONS_LANG3_VERSION=3.12.0

WORKDIR /app
USER root

# System deps + pip upgrade
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc wget zip ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --no-cache-dir --upgrade pip

# Snowflake connector JARs (host-independent — cache-friendly)
RUN wget -q -O /opt/bitnami/spark/jars/spark-snowflake_2.12-${SPARK_SNOWFLAKE_VERSION}.jar \
        https://repo1.maven.org/maven2/net/snowflake/spark-snowflake_2.12/${SPARK_SNOWFLAKE_VERSION}/spark-snowflake_2.12-${SPARK_SNOWFLAKE_VERSION}.jar \
    && wget -q -O /opt/bitnami/spark/jars/snowflake-jdbc.jar \
        https://repo1.maven.org/maven2/net/snowflake/snowflake-jdbc/${SNOWFLAKE_JDBC_VERSION}/snowflake-jdbc-${SNOWFLAKE_JDBC_VERSION}.jar \
    && chmod 644 /opt/bitnami/spark/jars/*.jar

# UCanAccess JARs (Microsoft Access .mdb / .accdb JDBC driver).
#
# Baked into the Spark jars dir — the same strategy used for Snowflake
# above — because in cluster mode the driver *and* every executor need
# the jars on their classpath, and the ``JavaPackage`` /
# ``spark.jars.packages`` injection path is not wired yet (the E2
# milestone). The four coordinates are: UCanAccess itself, jackcess
# (the Access reader), hsqldb (the relational engine UCanAccess maps
# tables onto), and commons-lang3 (a jackcess dependency).
RUN wget -q -O /opt/bitnami/spark/jars/ucanaccess-${UCANACCESS_VERSION}.jar \
        https://repo1.maven.org/maven2/net/sf/ucanaccess/ucanaccess/${UCANACCESS_VERSION}/ucanaccess-${UCANACCESS_VERSION}.jar \
    && wget -q -O /opt/bitnami/spark/jars/jackcess-${JACKCESS_VERSION}.jar \
        https://repo1.maven.org/maven2/com/healthmarketscience/jackcess/jackcess/${JACKCESS_VERSION}/jackcess-${JACKCESS_VERSION}.jar \
    && wget -q -O /opt/bitnami/spark/jars/hsqldb-${HSQLDB_VERSION}.jar \
        https://repo1.maven.org/maven2/org/hsqldb/hsqldb/${HSQLDB_VERSION}/hsqldb-${HSQLDB_VERSION}.jar \
    && wget -q -O /opt/bitnami/spark/jars/commons-lang3-${COMMONS_LANG3_VERSION}.jar \
        https://repo1.maven.org/maven2/org/apache/commons/commons-lang3/${COMMONS_LANG3_VERSION}/commons-lang3-${COMMONS_LANG3_VERSION}.jar \
    && chmod 644 /opt/bitnami/spark/jars/*.jar

# UCanAccess Spark JdbcDialect (bracket-quotes identifiers).
#
# UCanAccess parses double-quoted identifiers as *string literals*, so
# Spark's default JDBC dialect (which double-quotes column names) makes
# every Access column resolve to its own name for every row — a table
# read comes back filled with header names instead of data (and crashes
# ``getInt()`` on numeric columns). This ~30-line dialect brackets
# identifiers instead; ``ovid2.drivers.access.spark`` registers it before
# reading. Compiled here (the ``dialect`` stage below) against this
# image's own Spark jars and baked next to the UCanAccess driver jars so
# the driver JVM has it on the classpath. See
# ``docker/ucanaccess-dialect/UcanaccessDialect.java``.
COPY --from=dialect /out/ucanaccess-spark-dialect.jar \
        /opt/bitnami/spark/jars/ucanaccess-spark-dialect.jar
RUN chmod 644 /opt/bitnami/spark/jars/ucanaccess-spark-dialect.jar

# XOR shim (patches pycryptodome to restore legacy Crypto.Cipher.XOR API)
COPY docker/XOR.py /opt/bitnami/python/lib/python3.10/site-packages/Crypto/Cipher/

# Runtime directories (writable by uid 1001 in Bitnami images).
#
# ``work`` and ``logs`` are the two dirs a Spark ``Worker`` process
# writes to at executor-launch time:
#
#   * ``/opt/bitnami/spark/work/app-<id>/<executor>/`` — per-executor
#     scratch (JAR staging, stdout/stderr).
#   * ``/opt/bitnami/spark/logs/`` — worker's own log file.
#
# The Bitnami base image ships both as ``0775 ovid2:root``. When
# ``ovid2 tools copy`` / ``ovid2 etl`` runs the containers as the
# invoking host uid (``--user $UID:$GID``, so the read-only XDG mount
# with ``secrets.d/*.json`` mode ``0o600`` is legible), the worker
# process can neither create the app dir nor open a log file. Every
# executor launch then fails with
# ``IOException: Failed to create directory /opt/bitnami/spark/work/…``.
# After ``MAX_EXECUTOR_RETRIES`` (10) rapid failures the master flags
# the app FAILED and the driver crashes mid-init with the classic
# ``Master removed our application: FAILED`` +
# ``getServletHandlers on a running MetricsSystem`` +
# ``AppStatusListener NullPointerException`` cascade.
#
# ``ovid2.core.compose`` / ``ovid2.core.docker`` also bind-mount
# per-invocation dirs on top of these paths as defence in depth, so
# older images that predate this chmod remain usable.
RUN mkdir -p /docker_data/spark_tmp \
    && chmod -R 777 /docker_data /opt/bitnami/spark/tmp /opt/bitnami/spark/conf /opt/bitnami/spark/work /opt/bitnami/spark/logs /app

# Give uid 1001 a real passwd entry so ``java`` resolves ``user.home``
# via ``getpwuid()`` and Ivy / Hadoop / anything that calls
# ``System.getProperty("user.home")`` gets a real directory. Without
# this, ``spark-submit`` fails at startup with:
#     java.lang.IllegalArgumentException: basedir must be absolute: ?/.ivy2/local
# because Bitnami runs uid 1001 with an empty ``getpwuid`` result and
# Java stores ``user.home = "?"``. Bitnami's own spark entrypoint sets
# this up via nss-wrapper at run time; we bake it in permanently so
# ``ovid2 tools test-engine`` and any other "plain python + pyspark"
# invocation works without a wrapper.
RUN echo 'ovid2:x:1001:0:Ovid2:/docker_data/spark_tmp:/bin/false' >> /etc/passwd

ENV PYARROW_IGNORE_TIMEZONE=1

# Bitnami's base image exports ``PYTHONPATH=/opt/bitnami/spark/python/:`` but
# omits the ``py4j-*-src.zip`` that ``pyspark`` needs to reach the JVM
# gateway. Upstream Spark's ``bin/pyspark`` prepends the zip at launch, but
# ``ovid2 tools test-engine`` (and any other plain ``python3 -c 'import
# pyspark'`` in this image) does not go through that script. Bake the zip
# into ``PYTHONPATH`` here so every Python invocation in the image can
# import ``pyspark`` successfully. The entrypoint also re-expands the glob
# at runtime as a safety net when the ``py4j`` version bumps.
ENV PYTHONPATH=/opt/bitnami/spark/python/lib/py4j-0.10.9.5-src.zip:/opt/bitnami/spark/python/:

# The image runs as uid 1001, which has no ``/etc/passwd`` entry and
# therefore no writable ``HOME``. Bitnami's own Spark launcher never
# noticed because ``spark-submit`` sets Ivy/Hadoop temp paths explicitly,
# but a plain ``ovid2 tools test-engine`` (or any first-party invocation
# of ``SparkSession.builder.getOrCreate()``) trips
# ``java.lang.IllegalArgumentException: basedir must be absolute:
# ?/.ivy2/local`` when Ivy tries to resolve ``~/.ivy2``. Point ``HOME`` at
# the already-world-writable ``/docker_data/spark_tmp`` so Ivy, Hadoop's
# ``~/.hadoop``, and any well-behaved library that wants a home directory
# find one.
ENV HOME=/docker_data/spark_tmp

# --------------------------------------------------------------------------
# Build stage: build the wheel from source.
# --------------------------------------------------------------------------
FROM base AS builder

COPY pyproject.toml README.md /build/
COPY src /build/src
RUN pip3 install --no-cache-dir hatchling build \
    && cd /build \
    && python3 -m build --wheel --outdir /wheels

# --------------------------------------------------------------------------
# Final stage: install wheel + runtime deps.
# --------------------------------------------------------------------------
FROM base AS final

COPY --from=builder /wheels/*.whl /tmp/
RUN pip3 install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

COPY docker/entrypoint.sh /usr/local/bin/ovid2-entrypoint
RUN chmod 755 /usr/local/bin/ovid2-entrypoint

USER 1001
ENTRYPOINT ["/usr/local/bin/ovid2-entrypoint"]
CMD ["--help"]
