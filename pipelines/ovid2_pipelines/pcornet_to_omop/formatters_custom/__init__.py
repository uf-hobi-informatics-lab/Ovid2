"""Hand-written OMOP formatters for the PCORnet -> OMOP converter.

Reserved for OMOP output tables that need CONCEPT-domain routing or
multi-source fan-in beyond what a declarative ``semantic_join`` spec can
express (condition_occurrence, measurement, drug_exposure, observation,
procedure_occurrence).
"""

from __future__ import annotations
