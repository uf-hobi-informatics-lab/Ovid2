"""Contract: shipped transform specs are valid and use known ops.

Every YAML transform spec bundled under
``src/ovid2/resources/transforms/`` must:

* load through :func:`ovid2.transforms.spec.load_from_yaml`
  (JSON-schema valid),
* reference only ops registered in
  :data:`ovid2.transforms.registry.TRANSFORMS`.

Also exercises the schema's rejection of a malformed spec so the guard
itself is proven to bite.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _shared_specs_dir() -> Path:
    """Return the packaged shared-library transforms directory."""
    import ovid2

    return Path(ovid2.__file__).resolve().parent / "resources" / "transforms"


@pytest.mark.contract
def test_shipped_specs_load_and_reference_known_ops() -> None:
    """Every bundled transform spec is schema-valid with known ops."""
    from ovid2.transforms.registry import TRANSFORMS
    from ovid2.transforms.spec import load_from_yaml

    specs_dir = _shared_specs_dir()
    if not specs_dir.is_dir():
        pytest.skip("No shared transforms directory present.")

    yaml_files = sorted(specs_dir.glob("*.yaml"))
    assert yaml_files, "Expected at least one shipped transform spec."

    for path in yaml_files:
        spec = load_from_yaml(path)
        for step in spec.steps:
            assert step.op in TRANSFORMS, (
                f"Spec '{path.name}' references unknown op '{step.op}'."
            )


@pytest.mark.contract
def test_malformed_spec_is_rejected() -> None:
    """A spec missing required keys fails schema validation."""
    from ovid2.transforms.spec import (
        TransformSpecValidationError,
        load_from_dict,
    )

    with pytest.raises(TransformSpecValidationError):
        load_from_dict({"v": 1})  # missing 'steps'

    with pytest.raises(TransformSpecValidationError):
        load_from_dict({"v": 2, "steps": [{"op": "to_upper"}]})  # bad version
