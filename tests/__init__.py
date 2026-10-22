"""Test suite package marker.

The suite is organized as:

* ``tests/unit/``        — fast, in-process, no Spark session required.
* ``tests/contract/``    — static invariants over ``src/ovid2/``.
* ``tests/integration/`` — multi-component tests; require Spark.
* ``tests/helpers/``     — shared builders and assertions.
* ``tests/fixtures/``    — CSV/JSON test data.
"""
