"""Bundled vocabularies.

Two loading paths:

- **YAML tree (S3 onwards).** ``_yaml_loader.register_all()`` walks
  ``src/ovid2/resources/semantic/vocabularies/*.yaml`` and registers
  every cross-CDM vocabulary shipped with Ovid2. OMOP CONCEPT is one
  such file; LOINC, SNOMED, ICD-10-CM, ICD-9-CM, RxNorm, NDC each get
  their own YAML pointing at OMOP-derived slice CSVs.
- **Python side-effect modules (C1).**
  :mod:`ovid2.semantic.vocabularies.pcornet` walks each CDM's YAML
  valueset tree and registers dotted-lowercase valueset vocabularies
  (`pcornet_7_0.demographic.sex`, …). Kept as Python because valueset
  binding also happens in the same pass.

Adding a new cross-CDM vocabulary: drop a
``resources/semantic/vocabularies/<name>.yaml`` file (schema:
``resources/schemas/semantic_handle.v1.json``) and, optionally, a
``resources/semantic/bindings/<cdm>.yaml`` row for the CDM columns
that should point at it.

Adding a new CDM-scoped valueset: use ``ovid2 tools import-cdm``,
which writes ``resources/cdms/<cdm>/valuesets/<t>.<f>.yaml`` — the
pcornet-style Python loader picks it up on next import.
"""

from __future__ import annotations

from ovid2.semantic.vocabularies import _yaml_loader as _yaml_loader

# Register every packaged cross-CDM vocabulary + binding at import time.
_yaml_loader.register_all()
