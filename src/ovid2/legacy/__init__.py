"""Legacy-Ovid byte-parity layer.

Everything a pipeline needs to produce output identical to the legacy
``<legacy-ovid>`` ETL lives here, gated behind
``ETL_PROFILE = "legacy_ovid"`` in ``pipeline_settings.py``:

- :mod:`ovid2.legacy.profile` — profile resolution.
- :mod:`ovid2.legacy.csv_roundtrip` — stage-boundary CSV write/read emulation.
- :mod:`ovid2.legacy.udfs` — byte-exact ports of the ``CommonFuncitons`` UDFs.
- :mod:`ovid2.legacy.dedup` — the legacy deduplicator semantics.
- :mod:`ovid2.legacy.formatter` — the ``LegacyFormatter`` base used by
  ported partner formatters.

The package is Spark-only by design and is on the pyspark allow-list in
``tests/contract/test_no_pyspark_outside_engine.py``. See
the adr-legacy-parity-port design note.
"""
