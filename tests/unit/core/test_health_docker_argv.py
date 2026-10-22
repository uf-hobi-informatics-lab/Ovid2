"""Unit tests for the health/dq_checks docker argv builder.

Locks in that ``_build_health_argv`` forwards *either* a saved address
name *or* a run-time storage selection (connection + location) so a
reusable EDC run can select the database/schema at submit time.
"""

from __future__ import annotations

import argparse

import pytest

from ovid2.core.docker import _build_health_argv


def _ns(**kw) -> argparse.Namespace:
    base = dict(
        command="health",
        pipeline="pcornet_edc",
        batch_id="20260101",
        engine="pyspark",
        input_address=None,
        output_address=None,
        input_connection=None,
        input_location=None,
        output_connection=None,
        output_location=None,
        packages=["20r1"],
        parts=None,
        checks=None,
        prev_input_address=None,
        cdm=None,
        sf_database=None,
        sf_account=None,
        sf_schema=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.mark.unit
def test_health_argv_forwards_saved_addresses() -> None:
    argv = _build_health_argv(_ns(input_address="in_x", output_address="out_x"))
    assert argv[0] == "health"
    assert "--input_address" in argv
    assert argv[argv.index("--input_address") + 1] == "in_x"
    assert argv[argv.index("--output_address") + 1] == "out_x"
    assert "--input_connection" not in argv


@pytest.mark.unit
def test_health_argv_forwards_connection_and_location() -> None:
    argv = _build_health_argv(
        _ns(
            input_connection="prod_sf",
            input_location="CHAR_SITE/PUBLIC",
            output_connection="prod_sf",
            output_location="CHAR_SITE/OVID_RESULTS",
        )
    )
    assert argv[argv.index("--input_connection") + 1] == "prod_sf"
    assert argv[argv.index("--input_location") + 1] == "CHAR_SITE/PUBLIC"
    assert argv[argv.index("--output_connection") + 1] == "prod_sf"
    assert argv[argv.index("--output_location") + 1] == "CHAR_SITE/OVID_RESULTS"
    # No stray None address token.
    assert "--input_address" not in argv
    assert None not in argv


@pytest.mark.unit
def test_health_argv_defaults_pipeline_label() -> None:
    argv = _build_health_argv(_ns(input_connection="c", output_connection="c"))
    assert argv[argv.index("--pipeline") + 1] == "pcornet_edc"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run via pytest.")
