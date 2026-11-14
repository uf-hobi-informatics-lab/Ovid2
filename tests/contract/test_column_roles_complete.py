"""Contract: every catalogued op declares well-formed *column roles*.

The navigator's field lineage
(:mod:`ovid2.web.services.pipeline_lineage_svc`) draws which columns each
transform / mapper op / fixer touches from the role tables beside the
web-safe catalogues. A new op without a role would silently disappear
from the per-field view, and a role token naming a param the op does not
take would resolve to nothing — both fail here.
"""

from __future__ import annotations

from typing import Any

import pytest

from ovid2.config.column_roles import ROLE_KEYS, SCOPES, token_params
from ovid2.config.fixers_catalogue import FIXER_CATALOGUE, FIXER_COLUMN_ROLES, LEGACY_FIX_NAMES
from ovid2.config.mapper_ops_catalogue import MAPPER_OP_CATALOGUE, MAPPER_OP_COLUMN_ROLES
from ovid2.config.transforms_catalogue import TRANSFORM_CATALOGUE, TRANSFORM_COLUMN_ROLES

pytestmark = pytest.mark.contract


def _check_shape(name: str, role: dict[str, Any]) -> None:
    assert set(role) <= ROLE_KEYS, f"{name}: unknown role keys {set(role) - ROLE_KEYS}"
    assert role.get("scope") in SCOPES, f"{name}: scope must be one of {SCOPES}"
    for key in ("inplace", "reads", "writes", "exprs", "tables"):
        assert isinstance(role.get(key, []), list), f"{name}.{key} must be a list"


def test_every_transform_has_a_role() -> None:
    names = {str(e["name"]) for e in TRANSFORM_CATALOGUE}
    assert set(TRANSFORM_COLUMN_ROLES) == names, (
        f"missing: {sorted(names - set(TRANSFORM_COLUMN_ROLES))}; "
        f"phantom: {sorted(set(TRANSFORM_COLUMN_ROLES) - names)}"
    )
    schemas = {str(e["name"]): e["params_schema"] for e in TRANSFORM_CATALOGUE}
    for name, role in TRANSFORM_COLUMN_ROLES.items():
        _check_shape(name, role)
        params = set((schemas[name].get("properties") or {}).keys())
        assert token_params(role) <= params, (
            f"transform {name}: role names params {token_params(role) - params} "
            f"that are not in its params_schema"
        )


def test_every_mapper_op_has_a_role() -> None:
    assert set(MAPPER_OP_COLUMN_ROLES) == set(MAPPER_OP_CATALOGUE)
    for name, role in MAPPER_OP_COLUMN_ROLES.items():
        _check_shape(name, role)
        params = set((MAPPER_OP_CATALOGUE[name]["params_schema"].get("properties") or {}).keys())
        assert token_params(role) <= params, f"mapper op {name}: unknown params in role"


def test_every_fixer_has_a_role() -> None:
    assert set(FIXER_COLUMN_ROLES) == set(FIXER_CATALOGUE)
    for name, role in FIXER_COLUMN_ROLES.items():
        _check_shape(name, role)
        assert token_params(role) <= {"src1", "src2"}, (
            f"fixer {name}: roles may only reference src1 / src2 (or literal COLUMNS)"
        )


def test_legacy_fix_names_map_to_catalogued_fixers() -> None:
    for legacy, native in LEGACY_FIX_NAMES.items():
        assert native in FIXER_CATALOGUE, f"{legacy} → {native} is not a catalogued fixer"
