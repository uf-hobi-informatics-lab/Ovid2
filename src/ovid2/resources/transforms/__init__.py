"""Shared transform-spec library (package marker).

YAML files dropped in this directory are reusable transform chains
loadable by name from any pipeline via
:func:`ovid2.transforms.spec.load_spec`. Per-pipeline specs under
``pipelines/ovid2_pipelines/<pipeline>/transforms/<name>.yaml`` override
a shared spec of the same name.
"""
