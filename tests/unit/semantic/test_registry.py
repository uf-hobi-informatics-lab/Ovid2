"""Unit tests for the semantic registry and column bindings."""

from __future__ import annotations

from pathlib import Path

import pytest

import ovid2.semantic as semantic
from ovid2.semantic.handle import VocabularyHandle
from ovid2.semantic.registry import ColumnBinding, _clear


@pytest.fixture(autouse=True)
def _isolated_registry() -> None:
    """Snapshot the registry, run the test on a clean slate, then restore.

    Rebuilds bundled registrations by re-importing every vocabulary
    submodule after the clear. Reloading the top-level package alone is
    not enough because Python caches the already-imported submodules.

    Since phase S3, the ``vocabularies`` package's ``__init__.py``
    invokes ``_yaml_loader.register_all()`` at import time to load the
    YAML-authored vocabularies (OMOP CONCEPT, LOINC, SNOMED, …). The
    fixture reloads the package itself so that call re-runs.
    """
    import importlib
    import pkgutil

    import ovid2.semantic.vocabularies as vocabularies_pkg

    def _reregister_bundled() -> None:
        # Reload every module *inside* the package first (pcornet.py,
        # _yaml_loader.py, …). Reloading _yaml_loader alone does not
        # re-invoke register_all() — that call lives in the package's
        # __init__.py.
        for module_info in pkgutil.iter_modules(vocabularies_pkg.__path__):
            mod_name = f"ovid2.semantic.vocabularies.{module_info.name}"
            if mod_name in importlib.sys.modules:
                importlib.reload(importlib.sys.modules[mod_name])
            else:
                importlib.import_module(mod_name)
        # Then reload the package's __init__.py so its side-effect
        # imports (including register_all()) fire again.
        importlib.reload(vocabularies_pkg)

    _clear()
    _reregister_bundled()
    yield
    _clear()
    _reregister_bundled()


class TestRegistryLookup:
    """Basic register/get/registered semantics."""

    def test_bundled_omop_concept_is_registered(self) -> None:
        """The one bundled vocabulary must be present after package import."""
        assert "omop_concept" in semantic.registered("vocabulary")
        handle = semantic.get_vocabulary("omop_concept")
        assert handle.code_column == "concept_code"

    def test_registered_kinds_returns_vocabulary(self) -> None:
        """``vocabulary`` shows up as a registered kind."""
        assert "vocabulary" in semantic.registered_kinds()

    def test_get_unknown_kind_raises(self) -> None:
        """Looking up an unknown kind raises with a helpful message."""
        with pytest.raises(ValueError, match="Unknown semantic handle"):
            semantic.get("metric", "revenue")

    def test_get_unknown_vocabulary_raises(self) -> None:
        """Looking up an unknown vocabulary name raises."""
        with pytest.raises(ValueError, match="Unknown semantic handle"):
            semantic.get_vocabulary("nonexistent")

    def test_registered_unknown_kind_returns_empty_list(self) -> None:
        """Unregistered kinds return an empty list, not an error — open-ended."""
        assert semantic.registered("metric") == []


class TestRegistryRegistration:
    """Registering new handles."""

    def test_register_duplicate_raises(self, tmp_path: Path) -> None:
        """Re-registering the same ``(kind, name)`` without overwrite is an error."""
        h = VocabularyHandle(
            name="test_only_vocab",
            code_column="loinc_num",
            source_path=tmp_path / "loinc.csv",
        )
        semantic.register(h)
        with pytest.raises(ValueError, match="already registered"):
            semantic.register(h)

    def test_register_overwrite(self, tmp_path: Path) -> None:
        """``overwrite=True`` replaces the existing entry."""
        h1 = VocabularyHandle(
            name="test_only_vocab",
            version="1",
            code_column="loinc_num",
            source_path=tmp_path / "loinc.csv",
        )
        h2 = VocabularyHandle(
            name="test_only_vocab",
            version="2",
            code_column="loinc_num",
            source_path=tmp_path / "loinc.csv",
        )
        semantic.register(h1)
        semantic.register(h2, overwrite=True)
        assert semantic.get_vocabulary("test_only_vocab").version == "2"

    def test_get_vocabulary_type_narrows(self) -> None:
        """``get_vocabulary`` returns a :class:`VocabularyHandle` instance."""
        assert isinstance(semantic.get_vocabulary("omop_concept"), VocabularyHandle)


class TestColumnBindings:
    """Binding registration and lookup.

    Uses ``(cdm='pcornet_7_0', table='demographic', column='TEST_COL')``
    for test-created bindings so we never collide with the bundled S3
    bindings that pre-register real code-carrying columns
    (`diagnosis.DX`, `lab_result_cm.LAB_LOINC`, …).
    """

    _TABLE = "demographic"
    _COL_A = "TEST_COL_A"
    _COL_B = "TEST_COL_B"

    def test_bind_and_lookup(self) -> None:
        """A bound column can be found by ``(cdm, table, column)``."""
        b = ColumnBinding(
            cdm="pcornet_7_0",
            table=self._TABLE,
            column=self._COL_A,
            vocabulary="omop_concept",
            role="diagnosis_code",
        )
        semantic.bind_column(b)
        assert semantic.binding_for("pcornet_7_0", self._TABLE, self._COL_A) == b

    def test_lookup_missing_returns_none(self) -> None:
        """Unregistered columns return ``None`` (not an error)."""
        assert semantic.binding_for("pcornet_7_0", "diagnosis", "MISSING") is None

    def test_bind_duplicate_raises(self) -> None:
        """Re-binding the same column without overwrite is an error."""
        b = ColumnBinding(
            cdm="pcornet_7_0",
            table=self._TABLE,
            column=self._COL_A,
            vocabulary="omop_concept",
        )
        semantic.bind_column(b)
        with pytest.raises(ValueError, match="already registered"):
            semantic.bind_column(b)

    def test_bind_overwrite(self) -> None:
        """``overwrite=True`` replaces the binding."""
        b1 = ColumnBinding(
            cdm="pcornet_7_0",
            table=self._TABLE,
            column=self._COL_A,
            vocabulary="omop_concept",
            role="diagnosis_code",
        )
        b2 = ColumnBinding(
            cdm="pcornet_7_0",
            table=self._TABLE,
            column=self._COL_A,
            vocabulary="omop_concept",
            role="dx_code_v2",
        )
        semantic.bind_column(b1)
        semantic.bind_column(b2, overwrite=True)
        binding = semantic.binding_for("pcornet_7_0", self._TABLE, self._COL_A)
        assert binding is not None
        assert binding.role == "dx_code_v2"

    def test_bind_unknown_vocabulary_raises(self) -> None:
        """Referential integrity — cannot bind to a nonexistent vocabulary."""
        b = ColumnBinding(
            cdm="pcornet_7_0",
            table=self._TABLE,
            column=self._COL_A,
            vocabulary="nonexistent",
        )
        with pytest.raises(ValueError, match="unknown vocabulary"):
            semantic.bind_column(b)

    def test_bindings_for_cdm(self) -> None:
        """Filter bindings by CDM. Phase C1 pre-registers PCORnet valuesets
        and phase S3 pre-registers cross-CDM code bindings; this test
        asserts on the presence of the *newly added* rows on top of
        those.
        """
        semantic.bind_column(
            ColumnBinding(
                cdm="pcornet_7_0",
                table=self._TABLE,
                column=self._COL_A,
                vocabulary="omop_concept",
            )
        )
        semantic.bind_column(
            ColumnBinding(
                cdm="pcornet_7_0",
                table=self._TABLE,
                column=self._COL_B,
                vocabulary="omop_concept",
            )
        )
        bindings = semantic.bindings_for("pcornet_7_0")
        # Ordering invariant still holds.
        assert bindings == tuple(sorted(bindings, key=lambda b: (b.table, b.column)))
        # The two newly-added rows are present.
        added = {(b.table, b.column) for b in bindings}
        assert (self._TABLE, self._COL_A) in added
        assert (self._TABLE, self._COL_B) in added

    def test_bindings_for_cdm_and_table(self) -> None:
        """Filter bindings by CDM and table."""
        semantic.bind_column(
            ColumnBinding(
                cdm="pcornet_7_0",
                table=self._TABLE,
                column=self._COL_A,
                vocabulary="omop_concept",
            )
        )
        semantic.bind_column(
            ColumnBinding(
                cdm="pcornet_7_0",
                table=self._TABLE,
                column=self._COL_B,
                vocabulary="omop_concept",
            )
        )
        bindings = semantic.bindings_for("pcornet_7_0", table=self._TABLE)
        # The newly-added test columns must be present. Other
        # demographic valueset bindings from the pre-registered PCORnet
        # YAMLs (SEX, RACE, …) may also appear.
        added = {b.column for b in bindings}
        assert self._COL_A in added
        assert self._COL_B in added
        assert all(b.table == self._TABLE for b in bindings)
