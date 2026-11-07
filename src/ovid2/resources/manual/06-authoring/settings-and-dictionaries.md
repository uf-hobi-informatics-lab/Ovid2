---
title: Settings & dictionaries
summary: pipeline_settings.py binds the package to a CDM and an identity; dictionaries.py holds its value maps.
audience: [developer]
---

## `pipeline_settings.py`

`pipeline_settings.py` is a small Python module of plain constants.
Ovid2 reads these:

| Name | Meaning |
| --- | --- |
| `cdm` | target CDM (`"pcornet_7_0"`, `"omop_5_3"`). Defaults to `pcornet_7_0`. |
| `mappers_type` | legacy mapper family, for tables without a mapper spec yet |
| `DEFAULT_PIPELINE_DB_NAME` | default warehouse database for this source |
| `NETWORKID`, `NETWORK_NAME`, `DATAMARTID`, `DATAMART_NAME`, `DATAMART_PLATFORM`, `CDM_VERSION`, `DATAMART_CLAIMS`, `DATAMART_EHR` | PCORnet **HARVEST** identity |
| `DATAVANT_KEY` | the site key for hash-token generation |
| `DEFAULT_UPLOAD_TYPE`, `MERGE_DATABASE_NAME`, `MERGE_SCHEMA_NAME` | how output is merged into a cumulative database (`no_merge` by default) |
| `REQUIRES_FAMILY` | set to `"spark"` if custom formatters need Spark-only APIs; the package is then refused on DuckDB |
| `ETL_PROFILE` | `"legacy_ovid"` marks a generated legacy-parity port; don't set it by hand |

```python
cdm = "pcornet_7_0"
DEFAULT_PIPELINE_DB_NAME = "CHAR_MYHOSPITAL"

NETWORKID = "NET01"
NETWORK_NAME = "Example Network"
DATAMARTID = "NET01MYHOSP"
DATAMART_NAME = "My Hospital"
CDM_VERSION = "070"
```

> [!CAUTION]
> Never put credentials or salts in `pipeline_settings.py`. Per-pipeline
> secrets, such as the `hash_id` salt, go in
> `$XDG_CONFIG_HOME/ovid2/secrets.d/<pipeline>.json` (mode 0600).

## `dictionaries.py`

`dictionaries.py` holds plain `dict[str, str]` maps from source values
to standard values. By convention each one is named
`<table>_<column>_dict`, and the `translate_by_dict` mapper op refers to
it by that name:

```python
demographic_sex_dict = {
    "M": "M",
    "MALE": "M",
    "1": "M",
    "F": "F",
    "FEMALE": "F",
    "2": "F",
    "U": "UN",
    "UNKNOWN": "UN",
}

encounter_enc_type_dict = {
    "INPATIENT": "IP",
    "EMERGENCY": "ED",
    "OUTPATIENT": "AV",
    "TELEHEALTH": "TH",
}
```

Tips:

- Apply a `case_map` op with `mode: upper` before the lookup, and write
  the keys in upper case.
- Choose deliberately what happens to unmatched values. Either set
  `default: OT` to send them to *other*, or let them pass through
  unchanged and catch them with the **valueset conformance** health
  panel.
- Other fixers can also use dictionaries and lists. For example,
  `list_remover` takes a list of codes to drop.

Next: [Formatter specs](formatters.md).
