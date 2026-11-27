"""The DSL module must survive ``cloudpickle`` by value (Snowpark UDFs)."""

from __future__ import annotations

import subprocess
import sys

import cloudpickle

from ovid2.core.engines import dsl


def _probe() -> object:
    return dsl.active_engine()


def test_dsl_engine_slots_pickle_empty() -> None:
    """A UDF closing over the DSL pickles; the host engine is not shipped.

    The payload is loaded in a *separate* interpreter, as Snowflake does:
    unpickling by-value classes in-process makes cloudpickle patch the live
    classes (``_WindowProxy``) with copied globals, polluting later tests.
    """
    cloudpickle.register_pickle_by_value(dsl)
    try:
        with dsl.use_engine(object()):
            payload = cloudpickle.dumps(_probe)
    finally:
        cloudpickle.unregister_pickle_by_value(dsl)
    code = "import sys, cloudpickle; print(cloudpickle.loads(sys.stdin.buffer.read())())"
    out = subprocess.run(
        [sys.executable, "-c", code], input=payload, capture_output=True, check=True
    )
    assert out.stdout.decode().strip() == "None"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run with pytest.")
