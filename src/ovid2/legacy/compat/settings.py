"""Legacy ``common/settings.py`` constants used by partner formatters.

Only the names the ported formatters reference are reproduced (values are
copied verbatim from legacy). ``HARVEST_TABLE_INFO`` is intentionally
absent: it no longer exists in legacy ``settings.py``, so the legacy
LAMBDA_A/LAMBDA_C harvest formatters fail with ``NameError`` and produce no
``formatted_harvest.csv`` — the Ovid2 port fails identically.
"""

HARVEST_CDM_VERSION = "070"
MGMT_FIELDS_VALUE = "01"
