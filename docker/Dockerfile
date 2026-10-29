# Ovid2 runtime image.
#
# Multi-stage build:
#   * ``spark``   — the pip ``pyspark`` distribution on Python 3.14 (its
#                   ``jars/`` dir is what the dialect compiles against).
#   * ``dialect`` — compiles the UCanAccess Spark JdbcDialect (JDK stage).
#   * ``base``    — Python 3.14 + JRE 21 + PySpark 4.1 + system deps +
#                   Snowflake / UCanAccess / hadoop-aws / hadoop-azure JARs +
#                   dialect jar.
#   * ``final``   — installs the ``ovid2`` wheel built by hatch.
#
# Build (from repo root):
#   docker build -f docker/Dockerfile -t ovid2:${VERSION} .
#
# Spark layout: ``SPARK_HOME`` is pinned to ``/opt/spark``, a symlink to the
# pip-installed ``pyspark`` package (which ships ``bin/spark-class``,
# ``jars/`` …). Every container-side Spark path (compose master/worker
# commands, scratch-dir mounts) is rooted there — see
# ``ovid2.core.docker.SPARK_HOME_IN_CONTAINER``.
#
# Version pins: Spark is capped at 4.1 because that is the newest Spark the
# Snowflake Spark connector is published for (``spark-snowflake_2.13``).
# Python 3.14 is the newest CPython ``snowflake-snowpark-python`` supports.
# Java 21 is the only JRE Debian 13 (the python:3.14-slim base) packages;
# Spark 4.x supports 17 and 21.

ARG PYTHON_IMAGE=python:3.14-slim
ARG SPARK_VERSION=4.1.3

# --------------------------------------------------------------------------
# Spark stage: the pip pyspark distribution (shared by dialect + base).
# --------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS spark
ARG SPARK_VERSION
RUN pip3 install --no-cache-dir "pyspark==${SPARK_VERSION}" \
    && python3 -c "import pyspark, os; print(pyspark.__version__, os.path.dirname(pyspark.__file__))"

# --------------------------------------------------------------------------
# Dialect stage: compile the UCanAccess Spark JdbcDialect.
#
# Compiled in a throwaway JDK stage against this exact Spark's jars (copied
# from the ``spark`` stage) so the ``JdbcDialect`` subclass is
# binary-compatible with the runtime (Spark 4.1 / Scala 2.13). ``--release
# 17`` keeps the bytecode loadable on any Spark-4-supported JRE.
# --------------------------------------------------------------------------
FROM eclipse-temurin:21-jdk AS dialect
COPY --from=spark /usr/local/lib/python3.14/site-packages/pyspark/jars /spark-jars
COPY docker/ucanaccess-dialect/UcanaccessDialect.java /src/UcanaccessDialect.java
RUN mkdir -p /classes /out \
    && javac --release 17 -cp "/spark-jars/*" -d /classes /src/UcanaccessDialect.java \
    && jar cf /out/ucanaccess-spark-dialect.jar -C /classes . \
    && ls -l /out/ucanaccess-spark-dialect.jar

# --------------------------------------------------------------------------
# Base runtime stage.
# --------------------------------------------------------------------------
FROM spark AS base

# Snowflake Spark connector (Scala 2.13 / Spark 4.1) + the JDBC driver
# version that connector release is built against.
ARG SPARK_SNOWFLAKE_VERSION=3.2.3-spark_4.1
ARG SNOWFLAKE_JDBC_VERSION=4.0.2
# UCanAccess is the pure-JVM JDBC driver for Microsoft Access
# (.mdb / .accdb). It has runtime jar dependencies — jackcess (the
# Access file reader), hsqldb (the in-memory relational engine UCanAccess
# maps the Access tables onto), commons-lang3 (used by jackcess) and
# commons-logging (jackcess logs through JCL; Bitnami used to ship it,
# pip pyspark only ships the ``jcl-over-slf4j`` bridge).
ARG UCANACCESS_VERSION=5.0.1
ARG JACKCESS_VERSION=4.0.5
ARG HSQLDB_VERSION=2.5.0
ARG COMMONS_LANG3_VERSION=3.12.0
ARG COMMONS_LOGGING_VERSION=1.3.5
# Object-store connectors. Versions track the Hadoop line bundled in the
# pyspark wheel (``hadoop-client-api-3.4.2``): hadoop-aws 3.4 uses the AWS
# SDK v2 bundle + the S3 analytics accelerator; hadoop-azure (ABFS) needs
# the legacy azure-storage SDK + jetty-util-ajax + wildfly-openssl. The
# Bitnami Spark 3.3 image baked in hadoop-aws only — ABFS is new.
ARG HADOOP_VERSION=3.4.2
ARG AWS_SDK_V2_VERSION=2.29.52
ARG S3_ANALYTICS_ACCELERATOR_VERSION=1.2.1
ARG AZURE_STORAGE_VERSION=7.0.1
ARG JETTY_VERSION=9.4.57.v20241219
ARG WILDFLY_OPENSSL_VERSION=2.1.4.Final

ENV SPARK_HOME=/opt/spark \
    JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64 \
    PYSPARK_PYTHON=python3 \
    PYARROW_IGNORE_TIMEZONE=1
ENV PATH="${SPARK_HOME}/bin:${PATH}"

WORKDIR /app
USER root

# System deps. ``bash`` + ``wget`` back the compose master healthcheck and
# the host readiness probe; ``procps`` gives Spark's launch scripts ``ps``;
# ``libnss-wrapper`` lets ``--user $UID:$GID`` containers resolve
# ``getpwuid`` (``ovid2.core.docker.NSS_WRAPPER_LIB``); ``gcc`` builds any
# sdist-only wheel; ``tini`` is not needed (``exec`` chains keep PID 1 sane).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        openjdk-21-jre-headless bash wget procps ca-certificates gcc zip libnss-wrapper \
    && rm -rf /var/lib/apt/lists/* \
    && test -x "${JAVA_HOME}/bin/java" \
    && ln -s /usr/lib/x86_64-linux-gnu/libnss_wrapper.so /usr/local/lib/libnss_wrapper.so \
    && pip3 install --no-cache-dir --upgrade pip \
    && ln -s "$(python3 -c 'import os, pyspark; print(os.path.dirname(pyspark.__file__))')" "${SPARK_HOME}" \
    && mkdir -p "${SPARK_HOME}/work" "${SPARK_HOME}/logs" "${SPARK_HOME}/conf" "${SPARK_HOME}/tmp"

ARG MAVEN=https://repo1.maven.org/maven2

# Snowflake connector JARs (host-independent — cache-friendly)
RUN cd "${SPARK_HOME}/jars" \
    && wget -q "${MAVEN}/net/snowflake/spark-snowflake_2.13/${SPARK_SNOWFLAKE_VERSION}/spark-snowflake_2.13-${SPARK_SNOWFLAKE_VERSION}.jar" \
    && wget -q -O snowflake-jdbc.jar "${MAVEN}/net/snowflake/snowflake-jdbc/${SNOWFLAKE_JDBC_VERSION}/snowflake-jdbc-${SNOWFLAKE_JDBC_VERSION}.jar" \
    && chmod 644 ./*.jar

# Object-store connector JARs (``s3a://`` and ``abfss://``).
RUN cd "${SPARK_HOME}/jars" \
    && wget -q "${MAVEN}/org/apache/hadoop/hadoop-aws/${HADOOP_VERSION}/hadoop-aws-${HADOOP_VERSION}.jar" \
    && wget -q "${MAVEN}/software/amazon/awssdk/bundle/${AWS_SDK_V2_VERSION}/bundle-${AWS_SDK_V2_VERSION}.jar" \
    && wget -q "${MAVEN}/software/amazon/s3/analyticsaccelerator/analyticsaccelerator-s3/${S3_ANALYTICS_ACCELERATOR_VERSION}/analyticsaccelerator-s3-${S3_ANALYTICS_ACCELERATOR_VERSION}.jar" \
    && wget -q "${MAVEN}/org/apache/hadoop/hadoop-azure/${HADOOP_VERSION}/hadoop-azure-${HADOOP_VERSION}.jar" \
    && wget -q "${MAVEN}/com/microsoft/azure/azure-storage/${AZURE_STORAGE_VERSION}/azure-storage-${AZURE_STORAGE_VERSION}.jar" \
    && wget -q "${MAVEN}/org/eclipse/jetty/jetty-util-ajax/${JETTY_VERSION}/jetty-util-ajax-${JETTY_VERSION}.jar" \
    && wget -q "${MAVEN}/org/eclipse/jetty/jetty-util/${JETTY_VERSION}/jetty-util-${JETTY_VERSION}.jar" \
    && wget -q "${MAVEN}/org/wildfly/openssl/wildfly-openssl/${WILDFLY_OPENSSL_VERSION}/wildfly-openssl-${WILDFLY_OPENSSL_VERSION}.jar" \
    && chmod 644 ./*.jar

# UCanAccess JARs (Microsoft Access .mdb / .accdb JDBC driver).
#
# Baked into the Spark jars dir — the same strategy used for Snowflake
# above — because in cluster mode the driver *and* every executor need
# the jars on their classpath, and the ``JavaPackage`` /
# ``spark.jars.packages`` injection path is not wired yet (the E2
# milestone).
RUN cd "${SPARK_HOME}/jars" \
    && wget -q "${MAVEN}/net/sf/ucanaccess/ucanaccess/${UCANACCESS_VERSION}/ucanaccess-${UCANACCESS_VERSION}.jar" \
    && wget -q "${MAVEN}/com/healthmarketscience/jackcess/jackcess/${JACKCESS_VERSION}/jackcess-${JACKCESS_VERSION}.jar" \
    && wget -q "${MAVEN}/org/hsqldb/hsqldb/${HSQLDB_VERSION}/hsqldb-${HSQLDB_VERSION}.jar" \
    && wget -q "${MAVEN}/org/apache/commons/commons-lang3/${COMMONS_LANG3_VERSION}/commons-lang3-${COMMONS_LANG3_VERSION}.jar" \
    && wget -q "${MAVEN}/commons-logging/commons-logging/${COMMONS_LOGGING_VERSION}/commons-logging-${COMMONS_LOGGING_VERSION}.jar" \
    && chmod 644 ./*.jar

# UCanAccess Spark JdbcDialect (bracket-quotes identifiers).
#
# UCanAccess parses double-quoted identifiers as *string literals*, so
# Spark's default JDBC dialect (which double-quotes column names) makes
# every Access column resolve to its own name for every row — a table
# read comes back filled with header names instead of data (and crashes
# ``getInt()`` on numeric columns). This ~30-line dialect brackets
# identifiers instead; ``ovid2.drivers.access.spark`` registers it before
# reading. Compiled here (the ``dialect`` stage above) against this
# image's own Spark jars and baked next to the UCanAccess driver jars so
# the driver JVM has it on the classpath. See
# ``docker/ucanaccess-dialect/UcanaccessDialect.java``.
COPY --from=dialect /out/ucanaccess-spark-dialect.jar \
        /opt/spark/jars/ucanaccess-spark-dialect.jar
RUN chmod 644 "${SPARK_HOME}/jars/ucanaccess-spark-dialect.jar"

# Runtime directories (world-writable for arbitrary ``--user`` uids).
#
# ``work`` and ``logs`` are the two dirs a Spark ``Worker`` process
# writes to at executor-launch time:
#
#   * ``$SPARK_HOME/work/app-<id>/<executor>/`` — per-executor
#     scratch (JAR staging, stdout/stderr).
#   * ``$SPARK_HOME/logs/`` — worker's own log file.
#
# When ``ovid2 tools copy`` / ``ovid2 etl`` runs the containers as the
# invoking host uid (``--user $UID:$GID``, so the read-only XDG mount
# with ``secrets.d/*.json`` mode ``0o600`` is legible), the worker
# process must be able to create the app dir and open a log file, or
# every executor launch fails with
# ``IOException: Failed to create directory $SPARK_HOME/work/…`` and,
# after ``MAX_EXECUTOR_RETRIES`` (10) rapid failures, the master flags
# the app FAILED and the driver crashes mid-init.
#
# ``ovid2.core.compose`` / ``ovid2.core.docker`` also bind-mount
# per-invocation dirs on top of these paths as defence in depth.
RUN mkdir -p /docker_data/spark_tmp \
    && chmod -R 777 /docker_data "${SPARK_HOME}/tmp" "${SPARK_HOME}/conf" "${SPARK_HOME}/work" "${SPARK_HOME}/logs" /app

# Give uid 1001 a real passwd entry so ``java`` resolves ``user.home``
# via ``getpwuid()`` and Ivy / Hadoop / anything that calls
# ``System.getProperty("user.home")`` gets a real directory. Without
# this, ``spark-submit`` fails at startup with:
#     java.lang.IllegalArgumentException: basedir must be absolute: ?/.ivy2/local
# ``ovid2 tools test-engine`` and any other "plain python + pyspark"
# invocation relies on it.
RUN echo 'ovid2:x:1001:0:Ovid2:/docker_data/spark_tmp:/bin/false' >> /etc/passwd

# uid 1001 has no other home; point ``HOME`` at the already-world-writable
# ``/docker_data/spark_tmp`` so Ivy, Hadoop's ``~/.hadoop``, and any
# well-behaved library that wants a home directory find one.
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

# Executor-side SDK extras: the s3 / azure_blob / gcs Spark data planes and
# Snowpark staging strategies import boto3 / azure-storage-blob /
# google-cloud-storage in-container (Bitnami used to ship boto3 implicitly).
# ``pyspark`` is already present from the ``spark`` stage at the pinned
# version; pip leaves it untouched.
ARG OVID2_EXTRAS=s3,azure,gcs
COPY --from=builder /wheels/*.whl /tmp/
RUN set -e; whl="$(ls /tmp/*.whl)"; \
    pip3 install --no-cache-dir "${whl}[${OVID2_EXTRAS}]" && rm /tmp/*.whl \
    && python3 -c "import pyspark, sys; assert pyspark.__version__.startswith('4.1'), pyspark.__version__; print('python', sys.version.split()[0], 'pyspark', pyspark.__version__)"

COPY docker/entrypoint.sh /usr/local/bin/ovid2-entrypoint
RUN chmod 755 /usr/local/bin/ovid2-entrypoint

USER 1001
ENTRYPOINT ["/usr/local/bin/ovid2-entrypoint"]
CMD ["--help"]
