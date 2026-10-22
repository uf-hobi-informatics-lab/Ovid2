"""Pipeline settings for the example PCORnet-native pipeline.

Every identity value below is a placeholder. Replace them with your own
network / datamart identifiers before producing a real HARVEST table.
"""

mappers_type = "pcornet_mappers"

DEFAULT_PIPELINE_DB_NAME = "CHAR_EXAMPLE"

########### HARVEST ################

NETWORKID = "NET01"
NETWORK_NAME = "Example Network"
DATAMARTID = "NET01EXAMPLE"
DATAMART_NAME = "Example Datamart"
DATAMART_PLATFORM = "01"
CDM_VERSION = "070"
DATAMART_CLAIMS = "01"
DATAMART_EHR = "02"
MGMT_FIELDS_VALUE = "01"

########### DATAVANT ################

DATAVANT_KEY = "example"

########### Merge ################

DEFAULT_UPLOAD_TYPE = "no_merge"
MERGE_DATABASE_NAME = "CHAR_EXAMPLE"
MERGE_SCHEMA_NAME = "PUBLIC"
