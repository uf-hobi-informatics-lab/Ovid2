"""Declarative container requirements a driver may contribute.

The pre-Driver-refactor world buried per-peripheral container needs
in three different places:

- :mod:`ovid2.connections.disc_mounts` — auto-mount for disc addresses.
- ``docker/Dockerfile`` — baked-in Java jars for S3 + Azure.
- Hard-coded ``bootstrap.py`` / ``docker.py`` special cases for the
  nss-wrapper overlay and the Spark work/logs bind mounts.

Under the Driver refactor, each driver **declares** what it needs
through :meth:`Driver.container_requirements` /
:meth:`Driver.container_requirements_for`. The collector in
:mod:`ovid2.core.docker` and :mod:`ovid2.core.compose` walks every
driver referenced by a plan and merges its requirements into the
final docker-run / compose invocation.

The value objects here are deliberately data-only (frozen dataclasses,
no methods that touch the filesystem, no imports of pyspark or
vendor SDKs) so they can be enumerated by the web tier without
loading heavy dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContainerRequirement:
    """Marker base class — drivers return concrete subclasses.

    Kept as a sealed union of ``ExtraMount``, ``JavaPackage``, and
    ``EnvOverlay``. Adding a new subclass requires updating the
    collector in :mod:`ovid2.core.docker` and :mod:`ovid2.core.compose`
    to consume it.
    """


@dataclass(frozen=True)
class ExtraMount(ContainerRequirement):
    """One host→container bind-mount the driver needs.

    Fields mirror ``docker run -v host:container:mode``. The reserved
    container paths (``/data``, ``/app``, ``/ovid_credentials``,
    ``/usr/include/rsa``) are enforced by the collector, not here —
    a driver is allowed to *declare* an overlap and the collector
    will refuse to apply it.
    """

    host_path: str
    container_path: str
    mode: str = "rw"  # ``rw`` | ``ro``


@dataclass(frozen=True)
class JavaPackage(ContainerRequirement):
    """A Maven-coordinate jar the driver needs on the JVM classpath.

    Consumed by :class:`ovid2.core.engines.pyspark.PySparkEngine` and
    the compose builder to populate ``spark.jars.packages``. Purely
    additive — the base image continues to ship its bundled jars.

    Example: ``JavaPackage("org.apache.hadoop:hadoop-aws:3.3.2")``.
    """

    coordinate: str
    #: Optional set of ``spark.jars.excludes`` coordinates. Rare;
    #: needed when a package's transitive tree collides with the
    #: base image's own jars.
    excludes: tuple[str, ...] = ()


@dataclass(frozen=True)
class EnvOverlay(ContainerRequirement):
    """One environment variable the driver needs set in the container.

    ``value`` may be a literal string or a reference to a secret
    the container already knows about (``$SNOWFLAKE_PASSWORD``, …).
    The collector performs no interpolation; the string is passed
    through verbatim.
    """

    name: str
    value: str


@dataclass(frozen=True)
class MergedRequirements:
    """Aggregated container requirements for a whole ETL plan.

    Produced by :func:`collect_requirements` from a sequence of
    per-driver contributions. Downstream (docker, compose) consumes
    this instead of walking drivers directly, so the merge / dedup
    logic lives in exactly one place.
    """

    mounts: tuple[ExtraMount, ...] = ()
    java_packages: tuple[JavaPackage, ...] = ()
    env_overlays: tuple[EnvOverlay, ...] = ()


def collect_requirements(
    requirements: tuple[ContainerRequirement, ...] | list[ContainerRequirement],
) -> MergedRequirements:
    """Merge a flat sequence of requirements into typed buckets.

    De-duplicates identical entries (Java packages requested by two
    drivers land in the JVM once). Mount de-dup uses the exact
    ``(host_path, container_path, mode)`` triple; two mounts with the
    same container path but different host paths are both kept — the
    collector-side rules (e.g. reserved-path rejection) fire later.
    """
    mounts: list[ExtraMount] = []
    java: list[JavaPackage] = []
    envs: list[EnvOverlay] = []
    for req in requirements:
        if isinstance(req, ExtraMount) and req not in mounts:
            mounts.append(req)
        elif isinstance(req, JavaPackage) and req not in java:
            java.append(req)
        elif isinstance(req, EnvOverlay) and req not in envs:
            envs.append(req)
    return MergedRequirements(
        mounts=tuple(mounts),
        java_packages=tuple(java),
        env_overlays=tuple(envs),
    )


__all__ = [
    "ContainerRequirement",
    "EnvOverlay",
    "ExtraMount",
    "JavaPackage",
    "MergedRequirements",
    "collect_requirements",
]
