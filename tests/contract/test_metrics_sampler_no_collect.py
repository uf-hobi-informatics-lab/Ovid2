"""Contract: the metrics sampler never triggers a Spark action.

The sampler runs on a daemon thread that polls ``engine.get_metrics()``
every 500 ms while a run is in flight. It must NEVER call
``.collect()`` / ``.toPandas()`` / ``.take()`` / ``.first()`` /
``.count()`` — those would materialise a DataFrame and defeat the
lazy-Spark invariant. The general no-collect scanner
(``test_no_collect_in_src.py``) already covers ``core/`` recursively;
this test adds an explicit focused assertion on the sampler module +
every engine's ``get_metrics`` method so a regression here is diagnosed
by name instead of a generic scan report.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

FORBIDDEN_METHODS: frozenset[str] = frozenset({
    "collect", "toPandas", "take", "first", "count"
})


def _forbidden_calls(source: str) -> list[str]:
    """Return dotted call names in ``source`` that are forbidden."""
    tree = ast.parse(source)
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in FORBIDDEN_METHODS:
                hits.append(node.func.attr)
    return hits


@pytest.mark.contract
def test_metrics_sampler_has_no_eager_actions() -> None:
    """``metrics_sampler.py`` never calls a Spark eager action."""
    src_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "ovid2"
        / "core"
        / "metrics_sampler.py"
    )
    source = src_path.read_text(encoding="utf-8")
    hits = _forbidden_calls(source)
    assert not hits, (
        f"metrics_sampler.py contains forbidden calls: {sorted(set(hits))}. "
        f"The sampler must be metadata-only — see ADR "
        f"'adr-live-engine-metrics.md'."
    )


@pytest.mark.contract
def test_engine_get_metrics_has_no_eager_actions() -> None:
    """Every engine's ``get_metrics`` body avoids eager Spark actions."""
    from ovid2.core.engines.registry import ENGINE_TYPE_MAP, load_engine_class

    for type_name in sorted(ENGINE_TYPE_MAP):
        cls = load_engine_class(type_name)
        # Only inspect engines that override the base method — those
        # that inherit the default share the same source with the base
        # class and are covered by the class-level module scan.
        get_metrics = getattr(cls, "get_metrics", None)
        if get_metrics is None:
            continue
        try:
            source = inspect.getsource(get_metrics)
        except (OSError, TypeError):
            continue
        # ``inspect.getsource`` returns the method still indented as
        # it appears in the class body; parse a dedented copy so the
        # AST parser accepts it.
        source = textwrap.dedent(source)
        hits = _forbidden_calls(source)
        assert not hits, (
            f"Engine '{type_name}' get_metrics contains forbidden "
            f"calls: {sorted(set(hits))}"
        )
