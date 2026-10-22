"""``custom`` adapter.

Escape hatch for formatters that cannot be expressed declaratively. The
spec names a dotted path to a :class:`~ovid2.formatters.base.BaseFormatter`
subclass; this adapter instantiates it and returns whatever its
:meth:`format` method returns.

Custom classes must obey the same lazy-Spark invariant as the rest of
the pipeline — enforced by the ``test_no_collect_in_src`` contract
test on anything under ``src/ovid2/`` and by convention on
``ovid2_pipelines.*``.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from ovid2.formatters.adapters._sources import read_source
from ovid2.formatters.adapters.base import AdapterContext, BaseAdapter
from ovid2.formatters.base import BaseFormatter
from ovid2.formatters.spec import FormatterSpec, SourceSpec

if TYPE_CHECKING:
    from pyspark.sql import DataFrame


class CustomAdapter(BaseAdapter):
    """Adapter that delegates to a hand-written :class:`BaseFormatter`."""

    def resolve(self, spec: FormatterSpec, ctx: AdapterContext) -> DataFrame:
        if not spec.class_path:
            raise ValueError(f"custom adapter requires 'class'; spec '{spec.formatter}' has none.")

        module_name, _, class_name = spec.class_path.rpartition(".")
        if not module_name:
            raise ValueError(
                f"Invalid class path '{spec.class_path}' — must be dotted (e.g. 'ovid2_pipelines.demo.formatters_custom.vital.VitalFormatter')."
            )

        module = importlib.import_module(module_name)
        formatter_class = getattr(module, class_name)
        formatter = formatter_class()
        if not isinstance(formatter, BaseFormatter):
            raise TypeError(
                f"class '{spec.class_path}' must subclass ovid2.formatters.base.BaseFormatter."
            )

        # Give multi-source custom formatters access to the runtime context
        # (Spark session, input address, pipeline, batch) so they can read
        # sibling OMOP source files through ``read_source`` with correct
        # globs / excludes. Single-source formatters simply ignore it.
        formatter.set_context(ctx)

        # The custom class reads its own input via ``formatter.input_file_name``.
        primary_source = SourceSpec(
            name=spec.formatter,
            glob=formatter.input_file_name,
            lowercase_columns=True,
        )
        df = read_source(
            primary_source,
            spark=ctx.spark,
            input_address=ctx.input_address,
            pipeline=ctx.pipeline,
            batch_id=ctx.batch_id,
            engine=ctx.engine,
            input_address_name=ctx.input_address_name,
        )
        return formatter.format(df)
