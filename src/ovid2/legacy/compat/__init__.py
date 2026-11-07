"""Shims for the legacy ``common/*.py`` modules partner formatters import.

The legacy-formatter migrator rewrites ``from pcornet_cdm import …`` /
``from ovid_logging import …`` / ``from settings import *`` in ported
formatters to import from here, so the verbatim bodies keep resolving the
same names.
"""
