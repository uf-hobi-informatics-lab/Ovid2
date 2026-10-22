"""Concept-id dictionaries for the PCORnet -> OMOP converter.

Ported verbatim from the legacy converter's ``dictionaries.py``
(the legacy ``pcornetcdm_to_omop_conversion/dictionaries.py``). These
static maps turn small PCORnet coded values (SEX, RACE, HISPANIC,
DX_TYPE, …) into fixed OMOP concept ids. They are consumed by the
converter's formatters via ``translate_by_dict``-style SQL ``CASE``
projections (the formatters build the map inline so no separate map
transformation stage is required).

Vocabulary-driven code -> concept-id resolution (ICD/LOINC/RxNorm/… via
the OMOP CONCEPT + CONCEPT_RELATIONSHIP views) is *not* here — that goes
through the ``semantic_join`` adapter / ``translate_by_concept`` op.
"""

from __future__ import annotations

# --- Person: gender / race / ethnicity -------------------------------------

gender_concept_id_dict = {
    "A": "0",
    "F": "8532",
    "M": "8507",
    "NI": "0",
    "UN": "4214687",
    "OT": "0",
    "": "0",
}

gender_source_concept_id_dict = {
    "A": "44814664",
    "F": "44814665",
    "M": "44814666",
    "NI": "44814650",
    "UN": "44814653",
    "OT": "44814649",
    "": "44814653",
}

race_concept_id_dict = {
    "01": "8657",
    "02": "8515",
    "03": "8516",
    "04": "8557",
    "05": "8527",
    "06": "4212311",
    "07": "2090000003",
    "NI": "0",
    "UN": "4218674",
    "OT": "44814113",
    "": "0",
}

race_source_concept_id_dict = {
    "01": "44814654",
    "02": "44814655",
    "03": "44814656",
    "04": "44814657",
    "05": "44814658",
    "06": "44814659",
    "07": "44814660",
    "NI": "44814650",
    "UN": "44814653",
    "OT": "44814649",
    "": "44814653",
}

ethnicity_concept_id_dict = {
    "Y": "38003563",
    "N": "38003564",
    "R": "2090000003",
    "NI": "0",
    "UN": "2090000002",
    "OT": "9177",
    "": "0",
}

ethnicity_source_concept_id_dict = {
    "Y": "44814651",
    "N": "44814652",
    "R": "44814660",
    "NI": "44814650",
    "UN": "44814653",
    "OT": "44814649",
    "": "44814653",
}

# --- Diagnosis / condition type + status -----------------------------------

diagnosis_type_concept_id_dict = {
    "OD": "32817",
    "BI": "32821",
    "CL": "32810",
    "DR": "32858",
    "NI": "44814650",
    "UN": "44814653",
    "OT": "44814649",
    "": "44814653",
}

diagnosis_status_concept_id_dict = {
    "AD": "32890",
    "DI": "32896",
    "FI": "40492206",
    "NI": "44814650",
    "UN": "44814653",
    "OT": "44814649",
    "": "44814653",
}

# --- Condition (native PCORnet CONDITION table) type + status --------------

condition_type_concept_id_dict = {
    "PR": "32865",
    "HC": "32840",
    "RG": "32879",
    "PC": "32880",
    "DR": "32858",
    "NI": "44814650",
    "UN": "44814653",
    "OT": "44814653",
    "": "44814653",
}

condition_status_concept_id_dict = {
    "AC": "9181",
    "RS": "32840",
    "NI": "44814650",
    "UN": "44814653",
    "OT": "44814649",
    "": "44814653",
}

# --- Death: death_cause_source -> death_type_concept_id ---------------------

death_cause_to_death_type_concept_id_dict = {
    "L": "32817",
    "N": "32815",
    "D": "32885",
    "S": "32815",
    "T": "32879",
    "DR": "32880",
    "NI": "0",
    "UN": "0",
    "OT": "0",
    "": "0",
}

# --- Visit occurrence: encounter type / admitting source / discharge status -

enc_type_to_visit_concept_id_dict = {
    "AV": "38004207",
    "ED": "9203",
    "EI": "262",
    "IP": "9201",
    "IS": "44814710",
    "OS": "581385",
    "IC": "4127751",
    "OA": "44814711",
    "TH": "5083",
    "NI": "0",
    "UN": "0",
    "OT": "0",
    "": "0",
}

admitting_source_to_admitting_source_concept_id_dict = {
    "AF": "38004307",
    "AL": "8615",
    "AM": "4021968",
    "AV": "38004207",
    "ED": "9203",
    "HH": "38004519",
    "HO": "581476",
    "HS": "8546",
    "IP": "38004279",
    "NH": "38004198",
    "RH": "38004526",
    "RS": "32277",
    "SN": "8863",
    "SH": "8717",
    "IH": "8717",
    "NI": "0",
    "UN": "0",
    "OT": "0",
    "": "0",
}

discharge_status_to_discharge_concept_id_dict = {
    "AF": "38004307",
    "AL": "8615",
    "AV": "38004207",
    "AM": "4021968",
    "ED": "9203",
    "HH": "38004519",
    "HO": "581476",
    "HS": "8546",
    "IP": "38004279",
    "NH": "38004198",
    "RH": "38004526",
    "RS": "32277",
    "SN": "8863",
    "SH": "8717",
    "IH": "8717",
    "EX": "4216643",
    "NI": "0",
    "UN": "0",
    "OT": "0",
    "": "0",
}

# --- Observation period: ENR_BASIS -> period_type_concept_id (M2) ----------

period_type_concept_id_dict = {
    "I": "32813",
    "D": "2000000001",  # custom concept id used by the legacy converter
    "G": "32847",
    "A": "32880",
    "E": "32827",
    "": "0",
}

# --- Care site: FACILITY_TYPE -> place_of_service_concept_id (M2) -----------

place_of_service_concept_id_dict = {
    "ADULT_DAY_CARE_CENTER": "4184853",
    "AMBULANCE_BASED_CARE": "4014812",
    "AMBULATORY_CARE_SITE_OTHER": "4262840",
    "AMBULATORY_SURGERY_CENTER": "4236737",
    "CARE_OF_THE_ELDERLY_DAY_HOSPITAL": "4148493",
    "CHILD_DAY_CARE_CENTER": "4186892",
    "CONTAINED_CASUALTY_SETTING": "4260063",
    "DIALYSIS_UNIT_HOSPITAL": "763147",
    "ELDERLY_ASSESSMENT_CLINIC": "4076899",
    "EMERGENCY_DEPARTMENT_HOSPITAL": "42627881",
    "FEE_FOR_SERVICE_PRIVATE_PHYSICIANS_GROUP_OFFICE": "4024102",
    "FREE_STANDING_AMBULATORY_SURGERY_FACILITY": "4022507",
    "FREE_STANDING_BIRTHING_CENTER": "4239088",
    "FREE_STANDING_GERIATRIC_HEALTH_CENTER": "4216447",
    "FREE_STANDING_LABORATORY_FACILITY": "4262104",
    "FREE_STANDING_MENTAL_HEALTH_CENTER": "4201419",
    "FREE_STANDING_RADIOLOGY_FACILITY": "4071621",
    "HEALTH_ENCOUNTER_SITE_NOT_LISTED": "4192764",
    "HEALTH_MAINTENANCE_ORGANIZATION": "4217012",
    "HELICOPTER_BASED_CARE": "4234093",
    "HOSPICE_FACILITY": "42628597",
    "HOSPITAL_BASED_OUTPATIENT_CLINIC_OR_DEPARTMENT_OTHER": "0",
    "HOSPITAL_CHILDRENS": "4305507",
    "HOSPITAL_COMMUNITY": "4021523",
    "HOSPITAL_GOVERNMENT": "4195901",
    "HOSPITAL_LONG_TERM_CARE": "4137296",
    "HOSPITAL_MILITARY_FIELD": "4182729",
    "HOSPITAL_PRISON": "4076511",
    "HOSPITAL_PSYCHIATRIC": "4268912",
    "HOSPITAL_REHABILITATION": "4213182",
    "HOSPITAL_TRAUMA_CENTER": "",
    "HOSPITAL_VETERANS_ADMINISTRATION": "4167525",
    "HOSPITAL_AMBULATORY_SURGERY_FACILITY": "4239665",
    "HOSPITAL_BIRTHING_CENTER": "4199606",
    "HOSPITAL_OUTPATIENT_ALLERGY_CLINIC": "4233864",
    "HOSPITAL_OUTPATIENT_DENTAL_CLINIC": "4009124",
    "HOSPITAL_OUTPATIENT_DERMATOLOGY_CLINIC": "4291308",
    "HOSPITAL_OUTPATIENT_ENDOCRINOLOGY_CLINIC": "4252101",
    "HOSPITAL_OUTPATIENT_FAMILY_MEDICINE_CLINIC": "4135228",
    "HOSPITAL_OUTPATIENT_GASTROENTEROLOGY_CLINIC": "4242280",
    "HOSPITAL_OUTPATIENT_GENERAL_SURGERY_CLINIC": "4236039",
    "HOSPITAL_OUTPATIENT_GERIATRIC_HEALTH_CENTER": "4055912",
    "HOSPITAL_OUTPATIENT_GYNECOLOGY_CLINIC": "4330868",
    "HOSPITAL_OUTPATIENT_HEMATOLOGY_CLINIC": "4209422",
    "HOSPITAL_OUTPATIENT_IMMUNOLOGY_CLINIC": "4236674",
    "HOSPITAL_OUTPATIENT_INFECTIOUS_DISEASE_CLINIC": "4155183",
    "HOSPITAL_OUTPATIENT_MENTAL_HEALTH_CENTER": "4032930",
    "HOSPITAL_OUTPATIENT_NEUROLOGY_CLINIC": "4244378",
    "HOSPITAL_OUTPATIENT_OBSTETRICAL_CLINIC": "4210450",
    "HOSPITAL_OUTPATIENT_ONCOLOGY_CLINIC": "4233354",
    "HOSPITAL_OUTPATIENT_OPHTHALMOLOGY_CLINIC": "4299943",
    "HOSPITAL_OUTPATIENT_ORTHOPEDICS_CLINIC": "4301489",
    "HOSPITAL_OUTPATIENT_OTORHINOLARYNGOLOGY_CLINIC": "4051781",
    "HOSPITAL_OUTPATIENT_PAIN_CLINIC": "4265287",
    "HOSPITAL_OUTPATIENT_PEDIATRIC_CLINIC": "4290359",
    "HOSPITAL_OUTPATIENT_PERIPHERAL_VASCULAR_CLINIC": "4184130",
    "HOSPITAL_OUTPATIENT_REHABILITATION_CLINIC": "4293009",
    "HOSPITAL_OUTPATIENT_RESPIRATORY_DISEASE_CLINIC": "4237309",
    "HOSPITAL_OUTPATIENT_RHEUMATOLOGY_CLINIC": "4141300",
    "HOSPITAL_OUTPATIENT_UROLOGY_CLINIC": "4196700",
    "HOSPITAL_RADIOLOGY_FACILITY": "4194202",
    "HOSPITAL_SHIP": "4049639",
    "INDEPENDENT_AMBULATORY_CARE_PROVIDER_SITE_OTHER": "4214691",
    "LOCAL_COMMUNITY_HEALTH_CENTER": "4238722",
    "NURSING_HOME": "45496309",
    "PRIVATE_PHYSICIANS_GROUP_OFFICE": "4214465",
    "PRIVATE_RESIDENTIAL_HOME": "764901",
    "PSYCHOGERIATRIC_DAY_HOSPITAL": "4147082",
    "RESIDENTIAL_INSTITUTION": "764901",
    "RESIDENTIAL_SCHOOL_INFIRMARY": "4220654",
    "RURAL_HEALTH_CENTER": "4299787",
    "SEXUALLY_TRANSMITTED_DISEASE_HEALTH_CENTER": "4105698",
    "SKILLED_NURSING_FACILITY": "4164912",
    "SOLO_PRACTICE_PRIVATE_OFFICE": "4222172",
    "SPORTS_FACILITY": "45765876",
    "SUBSTANCE_ABUSE_TREATMENT_CENTER": "4061738",
    "TRAVELERS_AID_CLINIC": "4245026",
    "VACCINATION_CLINIC": "4263744",
    "WALK_IN_CLINIC": "4217918",
    "NI": "44814650",
    "UN": "44814653",
    "OT": "44814649",
    "": "44814653",
}

# --- Drug: route / immunization type (M3) ----------------------------------

route_concept_id_dict = {
    "OTIC": "4023156", "INTRA_ARTICULAR": "4006860", "GASTROSTOMY": "4132254",
    "JEJUNOSTOMY": "4133177", "NASOGASTRIC": "4132711", "SUBLESIONAL": "46270168",
    "VAGINAL": "4057765", "ORAL": "4132161", "SUBCUTANEOUS": "4142048",
    "RECTAL": "4290759", "DENTAL": "4163765", "ENDOCERVICAL": "4186831",
    "ENDOSINUSIAL": "4157756", "ENDOTRACHEOPULMONARY": "4186832",
    "EXTRA_AMNIOTIC": "4186833", "GASTROENTERAL": "4186834", "GINGIVAL": "4156704",
    "INTRAAMNIOTIC": "4163767", "INTRABURSAL": "4163768", "INTRACARDIAC": "4156705",
    "INTRACAVERNOUS": "4157757", "INTRACORONARY": "4186836", "INTRADERMAL": "4156706",
    "INTRADISCAL": "4163769", "INTRALESIONAL": "4157758", "INTRALYMPHATIC": "4157759",
    "INTRAOCULAR": "4157760", "INTRAPLEURAL": "4156707", "INTRASTERNAL": "4186837",
    "INTRAVESICAL": "4186838", "OROMUCOSAL": "4186839", "PERIARTICULAR": "4156708",
    "PERINEURAL": "4157761", "SUBCONJUNCTIVAL": "4163770", "INTRALUMINAL": "4292410",
    "SUBLINGUAL": "4292110", "INTRAPERITONEAL": "4243022", "TRANSMUCOSAL": "4232601",
    "INTRATRACHEAL": "4229543", "INTRABILIARY": "4223965", "EPIDURAL": "4225555",
    "SUBORBITAL": "4166865", "CAUDAL": "4220455", "INTRAOSSEOUS": "4213522",
    "INTRATHORACIC": "4167393", "ENTERAL": "4167540", "INTRADUCTAL": "4170083",
    "INTRATYMPANIC": "4168656", "INTRAVENOUS_CENTRAL": "4170113",
    "INTRAMYOMETRIAL": "4168038", "GASTRO_INTESTINAL_STOMA": "4168665",
    "COLOSTOMY": "4168047", "PERIURETHRAL": "4303646", "INTRACORONAL": "4303667",
    "RETROBULBAR": "4303673", "INTRACARTILAGINOUS": "4303676", "INTRAVITREAL": "4302785",
    "INTRASPINAL": "4302788", "OROGASTRIC": "4303795", "TRANSURETHRAL": "4305382",
    "INTRATENDINOUS": "4303939", "INTRACORNEAL": "4305690", "OROPHARYNGEAL": "4303277",
    "PERIBULBAR": "4304274", "NASOJEJUNAL": "4305834", "FISTULA": "4304277",
    "SURGICAL_DRAIN": "4304412", "INTRACAMERAL": "4303409", "PARACERVICAL": "4303515",
    "INTRASYNOVIAL": "4302352", "INTRADUODENAL": "4302354", "INTRACISTERNAL": "4305993",
    "INTRATESTICULAR": "4171067", "INTRACRANIAL": "4171079", "TUMOR_CAVITY": "4169472",
    "PARAVERTEBRAL": "4170267", "INTRASINAL": "4169440", "TRANSCERVICAL": "4304730",
    "SUBTENDINOUS": "4302493", "INTRAABDOMINAL": "4304882", "SUBGINGIVAL": "4306649",
    "INTRAOVARIAN": "4306657", "URETERAL": "4304571", "PERITENDINOUS": "4305564",
    "INTRABRONCHIAL": "4303263", "INTRAPROSTATIC": "4171725", "SUBMUCOSAL": "45956878",
    "SURGICAL_CAVITY": "4170771", "ILEOSTOMY": "4305679",
    "INTRAVENOUS_PERIPHERAL": "4171884", "PERIOSTEAL": "4171893",
    "ESOPHAGOSTOMY": "4172191", "UROSTOMY": "4170435", "LARYNGEAL": "4170440",
    "INTRAPULMONARY": "4169270", "MUCOUS_FISTULA": "4171243", "NASODUODENAL": "4172316",
    "BODY_CAVITY": "4222254", "INTRAVENTRICULAR_CARDIAC": "4222259",
    "INTRACEREBROVENTRICULAR": "4224886", "PERCUTANEOUS": "35627167",
    "INTERSTITIAL": "4327128", "ARTERIOVENOUS_GRAFT": "762840",
    "INTRAESOPHAGEAL": "40492284", "INTRAGINGIVAL": "40492286",
    "INTRAVASCULAR": "40492287", "INTRADURAL": "40492288", "INTRAMENINGEAL": "40492300",
    "INTRAGASTRIC": "40492301", "INTRACORPUS_CAVERNOSUM": "40492302",
    "INTRAPERICARDIAL": "40492305", "INTRALINGUAL": "40493227", "INTRAHEPATIC": "40493258",
    "CONJUNCTIVAL": "40486444", "INTRAEPICARDIAL": "40487473",
    "TRANSENDOCARDIAL": "40487850", "TRANSPLACENTAL": "40487858",
    "INTRACEREBRAL": "40488317", "INTRAILEAL": "40490837", "PERIODONTAL": "40490866",
    "PERIDURAL": "40490896", "LOWER_RESPIRATORY_TRACT": "40490898",
    "INTRAMAMMARY": "40491321", "INTRATUMOR": "40491322", "TRANSTYMPANIC": "40491830",
    "TRANSTRACHEAL": "40491832", "RESPIRATORY_TRACT": "40486069",
    "DIGESTIVE_TRACT": "40487501", "INTRAEPIDERMAL": "40487983",
    "INTRAJEJUNAL": "40489989", "INTRACOLONIC": "40489990", "CUTANEOUS": "40490507",
    "TRANSDERMAL": "4262099", "NASAL": "4262914", "INTRAVENOUS": "4171047",
    "BUCCAL": "4181897", "OPHTHALMIC": "4184451", "INTRA_ARTERIAL": "4240824",
    "INTRAMEDULLARY": "4246511", "TOPICAL": "4263689", "INTRAUTERINE": "4269621",
    "ARTERIOVENOUS_FISTULA": "44783786", "INTRANEURAL": "46272911",
    "INTRAMURAL": "46272926", "EXTRACORPOREAL": "37018288", "INTRATHECAL": "4217202",
    "INTRAMUSCULAR": "4302612", "URETHRAL": "4233974",
    "NI": "0", "UN": "0", "OT": "0", "": "0",
}

immunization_type_concept_id_dict = {
    "OD": "32818", "EF": "32849", "IS": "32849", "PR": "32865", "DR": "32880",
    "NI": "0", "UN": "0", "OT": "0",
}

# --- Specimen: SPECIMEN_SOURCE -> specimen_concept_id (M3) -------------------

specimen_to_specimen_concept_id_dict = {
    "^BPU": "4001346", "^EMBRYO": "42605994", "^FETUS": "4202977",
    "^MUSHROOM_SPECIMEN": "4002736", "^PATIENT": "4001345",
    "^PLANT_SPECIMEN": "4000612", "^SPECIMEN": "4048506", "^TICK": "46271815",
    "ABSCESS": "4001183", "ADRENAL_GLAND": "4204948", "AIR": "40487357",
    "AMNIO_FLD": "4002223", "ANAL": "4204349", "AORTA.ROOT": "42535844",
    "BARTHOLIN_CYST": "43021092", "BBL": "4001347", "BLD": "4001225",
    "BLD^BPU": "4001346", "BLD^CONTROL": "4046276", "BLD^DONOR": "4047497",
    "BLD^PATIENT": "4046277", "BLD_BONE_MAR": "4000623", "BLDA": "4047496",
    "BLDC": "4046834", "BLDCO": "4046835", "BLDCOA": "45766301", "BLDCOV": "45766302",
    "BLDMV": "4001226", "BLDP": "4047495", "BLDV": "4045667", "BODY_FLD": "4204181",
    "BONE": "4123176", "BONE_MAR": "4000623", "BONE_MARROW": "4000623",
    "BRAIN": "4002227", "BREAST": "4132242", "BRONCHIAL": "4206409",
    "BUCCAL_SMEAR": "4120353", "BURN": "4123177", "CALCULUS": "4001065",
    "CELLS.XXX": "44806863", "CHEESE": "42605987", "CNJT": "4000637", "CNL": "4002870",
    "COL": "4002874", "COLON": "4002892", "CONTACT_LENS": "4207128", "CRN": "4000636",
    "CSF": "4124259", "CSF.SPUN": "45773106", "CTP": "4001350", "CVM": "4046281",
    "CVX_VAG": "45765695", "DAIRY_PRODUCT": "42606009", "DENTIN": "4001358",
    "DIAL_FLD": "4000624", "DIAL_FLD_PRT": "4045763", "DUOD_FLD": "4046839",
    "EAR": "4204951", "EAR_FLUID": "37116371", "EGG": "42573743", "EGGYLK": "4002220",
    "ENDOCERVICAL_BRUSH": "36713736", "ENDOMET": "4002226", "ENVIR": "4206699",
    "ENVIRONMENTAL_SPECIMEN": "4206699", "ESOPHAGEAL_BRUSH": "36713736",
    "EXHL_GAS": "4001357", "EXUDATE": "4122250", "EYE": "4001190", "FEATHER": "42605999",
    "FIBROBLASTS": "4001356", "FLU.NONBIOLOGICAL": "4001351", "FOOD": "4000616",
    "GAST_FLD": "4120336", "GENITAL": "4001063", "GENITAL_FLD": "46270237",
    "GENITAL_LOC": "4048850", "GENITAL_MUC": "4046840", "HAIR": "4001355",
    "INHL_GAS": "4002878", "KIDNEY": "4133742", "LIVER": "4002224", "LUNG": "4133172",
    "LUNG_TISS": "4164332", "LYMPH_NODE": "4124291", "MECONIUM": "4001060",
    "MILK": "4001058", "MLK.RAW": "42606004", "MOUTH": "4204956", "MUSCLE": "45767596",
    "NAIL": "4000618", "NASAL_FLUID": "4057744", "NIPPLE_DISCHARGE": "4202978",
    "NOSE": "40491357", "NPH": "42539412", "OVARY": "4027387", "PANCREAS": "4133175",
    "PENIS": "4002896", "PERITONEUM": "4325355", "PHARYNX": "4332381",
    "PLACENTA": "4001192", "PLANT": "4000612", "PLAS": "4000626", "PLATELETS": "4001179",
    "PLEURA": "4133739", "PLR_FLD": "4302933", "PPP": "4000627", "PPP^CONTROL": "4046842",
    "PPP^POOL": "42537788", "PREPUTIAL_WASH": "42606035", "PROSTATE": "4001186",
    "PROSTATIC_FLD": "4119384", "PRP": "4001180", "PRP^CONTROL": "4046842",
    "PUS": "4000617", "RECTUM": "40487491", "RESPIRATORY": "4119536",
    "RESPIRATORY.LOWER": "4119538", "RESPIRATORY.UPPER": "4119537", "SALIVA": "4001062",
    "SEMEN": "44808818", "SER": "4001181", "SER^CONTROL": "42539680",
    "SER^DONOR": "4048853", "SER_PLAS_BLD": "37303842", "SER_PLAS": "37303842",
    "SKIN": "43531265", "SOFT_TISSUE.FNA": "4205805", "SPECIMEN": "4048506",
    "SPERMATOZOA": "4002882", "SPLEEN": "4336060", "SPTT": "4122289", "SPUTUM": "4002876",
    "STOMACH": "4002891", "STOOL": "4002879", "SWEAT": "4046838", "SYNV_FLD": "4002875",
    "TEAR": "4046369", "TESTIS": "4134447", "THYROID": "4164619", "TRAC": "4002894",
    "TRACHEAL_SWAB": "42573143", "TSMI": "4046380", "TUMOR": "4122248", "ULC": "4000629",
    "URETHRA": "4001188", "URINARY_BLADDER": "42872877", "URINE": "4046280",
    "URINE_SED": "4045758", "UTERUS": "4133745", "VAG": "4001189", "VOMITUS": "4045760",
    "WATER": "4001352", "WBC": "4122287", "WHEY": "764638", "WOUND": "4002221",
    "WOUND.DEEP": "46270190", "XXX.BODY_FLUID": "4204181", "XXX.SWAB": "4120698",
    "NI": "44814650", "UN": "44814653", "OT": "44814649",
}

# --- Clinical result units -> unit_concept_id (M3; shared lab/obs) ----------
# Large UCUM -> OMOP unit concept map. Consumed by the obs_clin / obs_gen /
# vital "common" fan-in and the measurement domain-routing.

result_unit_to_unit_concept_id_dict = {
    "[APLU]": "9099", "[APLU]/mL": "9156", "[arbU]": "9260", "[arbU]/mL": "8980",
    "[AU]": "45744811", "[bdskU]": "9262", "[bethU]": "9161", "[CFU]": "9278",
    "[CFU]/mL": "9423", "[Ch]": "9279", "[cin_i]": "9283", "[degF]": "9289",
    "[dr_av]": "9295", "[drp]": "9296", "[foz_us]": "9304", "[ft_i]": "9306",
    "[gal_us]": "9515", "[GPLU]": "9100", "[GPLU]/mL": "9157", "[in_iH2O]": "9328",
    "[in_i]": "9327", "[IU]": "8718", "[IU]/dL": "9332", "[IU]/g": "9333",
    "[IU]/g{Hb}": "9334", "[IU]/h": "9687", "[IU]/kg": "9335", "[IU]/L": "8923",
    "[IU]/mL": "8985", "[kaU]": "9339", "[knkU]": "9342", "[mclgU]": "9358",
    "[mi_i]": "9362", "[MPLU]": "9101", "[MPLU]/mL": "9158", "[oz_av]": "9373",
    "[oz_tr]": "9374", "[pH]": "8482", "[ppb]": "8703", "[ppm]": "9387",
    "[ppth]": "9154", "[pptr]": "9155", "[psi]": "9389", "[pt_us]": "9391",
    "[qt_us]": "9394", "[sft_i]": "9403", "[sin_i]": "9404", "[syd_i]": "9411",
    "[tbU]": "9413", "[tbs_us]": "9412", "[toddU]": "9415", "[tsp_us]": "9416",
    "[yd_i]": "9420", "{CAEU}": "8998", "{cells}": "45744812",
    "{cells}/[HPF]": "8889", "{cells}/uL": "8784", "{copies}/mL": "8799",
    "{CPM}": "8483", "{EhrlichU}": "8480", "{EhrlichU}/100.g": "9425",
    "{EhrlichU}/dL": "8829", "{EIAU}": "8556", "{ISR}": "8488", "{JDFU}": "8560",
    "{M.o.M}": "8494", "{ratio}": "8523", "{RBC}/uL": "9428", "{s_co_ratio}": "8779",
    "{spermatozoa}/mL": "9430", "{titer}": "8525", "/[arbU]": "9235", "/[HPF]": "8786",
    "/[LPF]": "8765", "/{entity}": "9236", "/10*10": "9238", "/10*12": "9239",
    "/10*12{RBCs}": "9240", "/10*6": "9241", "/10*9": "9242", "/100": "9243",
    "/100{spermatozoa}": "9244", "/100{WBCs}": "9032", "/a": "44777560", "/dL": "9245",
    "/g": "9246", "/g{creat}": "9247", "/g{Hb}": "9248", "/g{tot_nit}": "9249",
    "/g{tot_prot}": "9250", "/g{wet_tis}": "9251", "/h": "44777557", "/kg": "9252",
    "/L": "9254", "/m2": "9255", "/m3": "44777558", "/mg": "9256", "/min": "8541",
    "/mL": "9257", "/mo": "44777658", "/s": "44777659", "/uL": "8647", "/wk": "44777559",
    "%": "8554", "%{abnormal}": "9216", "%{activity}": "8687", "%{bacteria}": "9227",
    "%{basal_activity}": "9217", "%{baseline}": "8688", "%{binding}": "9218",
    "%{blockade}": "9219", "%{bound}": "9220", "%{excretion}": "9223", "%{Hb}": "8737",
    "%{hemolysis}": "9226", "%{inhibition}": "8738", "%{pooled_plasma}": "9681",
    "%{positive}": "9231", "%{saturation}": "8728", "%{total}": "8632",
    "%{uptake}": "8649", "%{vol}": "9234", "%{WBCs}": "9229", "10*12/L": "8734",
    "10*3": "8566", "10*3{copies}/mL": "9437", "10*3{RBCs}": "9434", "10*3/L": "9435",
    "10*3/mL": "9436", "10*3/uL": "8848", "10*4/uL": "32706", "10*5": "9438",
    "10*6": "8549", "10*6/L": "9442", "10*6/mL": "8816", "10*6/uL": "8815",
    "10*8": "9443", "10*9/L": "9444", "10*9/mL": "9445", "10*9/uL": "9446", "a": "9448",
    "A": "8543", "ag/{cell}": "32695", "atm": "9454", "bar": "9464", "Bq": "9469",
    "cal": "9472", "Cel": "586323", "cg": "9479", "cL": "9482", "cm": "8582",
    "cm[H2O]": "44777590", "cm2": "9483", "cP": "8479", "d": "8512", "dB": "44777591",
    "deg": "9484", "dg": "9485", "dL": "9486", "dm": "9487", "eq": "9489",
    "eq/L": "9490", "eq/mL": "9491", "eq/mmol": "9492", "eq/umol": "9493", "erg": "9494",
    "eV": "9495", "F": "8517", "fg": "9496", "fL": "8583", "fm": "9497", "fmol": "9498",
    "fmol/g": "9499", "fmol/L": "8745", "fmol/mg": "9500", "fmol/mL": "9501", "g": "8504",
    "g.m": "9504", "g{creat}": "44777596", "g{Hb}": "44777597",
    "g{total_prot}": "44777599", "g/(100.g)": "9508", "g/(12.h)": "44777593",
    "g/(24.h)": "8807", "g/(5.h)": "8791", "g/{total_weight}": "9509", "g/cm3": "45956701",
    "g/dL": "8713", "g/g": "9510", "g/g{creat}": "9511", "g/h": "44777594", "g/kg": "9512",
    "g/L": "8636", "g/m2": "9513", "g/mL": "9514", "g/mmol": "32702", "Gy": "9519",
    "h": "8505", "H": "8518", "Hz": "9521", "J": "9522", "K": "9523", "K/W": "9524",
    "kat": "9526", "kat/kg": "9527", "kat/L": "44777601", "kcal/[oz_av]": "9528",
    "kg": "9529", "kg/L": "9530", "kg/m2": "9531", "kg/m3": "9532", "kg/mol": "9533",
    "kL": "9535", "km": "9536", "kPa": "44777602", "ks": "9537", "L": "8519",
    "L/(24.h)": "8857", "L/h": "44777603", "L/kg": "9542", "L/L": "44777604",
    "L/min": "8698", "L/s": "32700", "lm": "9543", "m": "9546", "m/s": "44777606",
    "m/s2": "44777607", "m2": "8617", "meq": "9551", "meq/{specimen}": "9552",
    "meq/dL": "9553", "meq/g": "9554", "meq/g{creat}": "9555", "meq/kg": "9556",
    "meq/L": "9557", "meq/m2": "9558", "meq/mL": "9559", "mg": "8576",
    "mg{FEU}/L": "44777663", "mg/(12.h)": "8908", "mg/(24.h)": "8909",
    "mg/(72.h)": "45891022", "mg/{total_volume}": "9560", "mg/dL": "8840", "mg/g": "8723",
    "mg/g{creat}": "9017", "mg/h": "44777610", "mg/kg": "9562", "mg/kg/h": "9691",
    "mg/kg/min": "9692", "mg/L": "8751", "mg/m2": "9563", "mg/m3": "9564", "mg/mg": "9565",
    "mg/mg{creat}": "9074", "mg/min": "44777611", "mg/mL": "8861", "mg/mmol": "44777612",
    "mg/mmol{creat}": "9075", "min": "9211", "mL": "8587", "mL/(12.h)": "44777664",
    "mL/(24.h)": "8930", "mL/{beat}": "9569", "mL/dL": "9570", "mL/h": "44777613",
    "mL/kg": "9571", "mL/min": "8795", "mL/min/{1.73_m2}": "9117", "mL/s": "44777614",
    "mm": "8588", "mm[Hg]": "8876", "mm/h": "8752", "mm2": "9572", "mmol": "9573",
    "mmol/(24.h)": "8910", "mmol/{specimen}": "44777615", "mmol/{total_vol}": "9574",
    "mmol/dL": "9575", "mmol/g": "9576", "mmol/g{creat}": "9018", "mmol/kg": "9577",
    "mmol/L": "8753", "mmol/m2": "9578", "mmol/mmol": "44777618",
    "mmol/mmol{creat}": "44777619", "mmol/mol": "9579", "mmol/mol{creat}": "9019",
    "mo": "9580", "mol": "9584", "mol/kg": "9585", "mol/L": "9586", "mol/m3": "9587",
    "mol/mL": "9588", "mol/s": "44777621", "mosm": "8605", "mosm/kg": "8862",
    "mosm/L": "9591", "mPa": "44777623", "mPa.s": "44777622", "ms": "9593", "Ms": "9592",
    "N": "9599", "ng": "9600", "ng{FEU}/mL": "32707", "ng/(24.h)": "44777624",
    "ng/dL": "8817", "ng/g": "8701", "ng/g{creat}": "9601", "ng/kg": "9602", "ng/L": "8725",
    "ng/m2": "9603", "ng/mg": "8818", "ng/mg{creat}": "44777625", "ng/mg{prot}": "9604",
    "ng/mL": "8842", "ng/mL{RBCs}": "9113", "ng/mL/h": "9020", "nkat": "44777626",
    "nL": "9606", "nm": "8577", "nmol": "9607", "nmol/(24.h)": "44777627", "nmol/dL": "9608",
    "nmol/g": "9609", "nmol/g{creat}": "9610", "nmol/h/L": "44777630", "nmol/L": "8736",
    "nmol/mg": "9611", "nmol/mg{creat}": "44777634", "nmol/min/mL": "44777635",
    "nmol/mL": "8843", "nmol/mmol": "9612", "nmol/mmol{creat}": "9063", "nmol/mol": "9614",
    "nmol/s": "44777637", "nmol/s/L": "8955", "ns": "9616", "Ohm": "9618", "osm": "9619",
    "osm/kg": "9620", "osm/L": "9621", "Pa": "9623", "pg": "8564", "pg/{cell}": "8704",
    "pg/dL": "8820", "pg/L": "9625", "pg/mL": "8845", "pg/mm": "9626", "pkat": "44777639",
    "pL": "9628", "pm": "9629", "pmol": "9630", "pmol/(24.h)": "44777640", "pmol/dL": "9631",
    "pmol/L": "8729", "pmol/mL": "9632", "pmol/umol": "9633", "ps": "9634", "s": "9212",
    "S": "9639", "Sv": "9645", "t": "9647", "T": "9646", "ueq": "9653", "ueq/L": "8875",
    "ueq/mL": "9654", "ug": "9655", "ug{FEU}/mL": "8989", "ug/(100.g)": "9656",
    "ug/(24.h)": "8906", "ug/{specimen}": "9657", "ug/dL": "8837", "ug/dL{RBCs}": "9659",
    "ug/g": "8720", "ug/g{creat}": "9014", "ug/g{Hb}": "9661", "ug/h": "44777645",
    "ug/kg": "9662", "ug/kg/h": "9690", "ug/kg/min": "9688", "ug/L": "8748", "ug/m2": "9663",
    "ug/mg": "8838", "ug/mg{creat}": "9072", "ug/min": "8774", "ug/mL": "8859",
    "ug/mmol": "32408", "ug/mmol{creat}": "44777646", "ug/ng": "9664", "ukat": "44777647",
    "uL": "9665", "um": "9666", "umol": "9667", "umol/(24.h)": "8907", "umol/dL": "8839",
    "umol/g": "9668", "umol/g{creat}": "9015", "umol/g{Hb}": "9669", "umol/kg": "44777654",
    "umol/L": "8749", "umol/mg": "9670", "umol/mg{creat}": "9671", "umol/min": "44777655",
    "umol/min/g": "9672", "umol/mL": "9673", "umol/mmol": "44777656",
    "umol/mmol{creat}": "9073", "umol/mol": "9674", "umol/mol{creat}": "9675",
    "umol/mol{Hb}": "44777657", "us": "9676", "V": "9677", "Wb": "9679", "wk": "8511",
    "inches": "9330", "pounds": "8739", "mmHg": "8876",
    "NI": "44814650", "UN": "44814653", "OT": "44814649",
}

operator_concept_id_dict = {
    "EQ": "4172703", "GE": "4171755", "GT": "4172704", "LE": "4171754",
    "LT": "4171756", "TX": "4035566", "NI": "4035566", "UN": "44814653",
    "OT": "44814649", "": "44814653",
}

# LAB_RESULT_SOURCE -> measurement (common) type concept id (M3).
lab_result_source_to_type_concept_id_dict = {
    "OD": "32817", "BI": "32821", "CL": "32810", "DR": "45754907",
    "NI": "0", "UN": "0", "OT": "0", "": "0",
}

result_qual_to_value_as_concept_id_dict = {
    "POSITIVE": "9191", "NEGATIVE": "9189", "BORDERLINE": "4162852",
    "ELEVATED": "4328749", "HIGH": "4328749", "LOW": "4267416", "NORMAL": "4124457",
    "ABNORMAL": "4183448", "UNDETERMINED": "4160775", "UNDETECTABLE": "9190",
    "DETECTED": "4126681", "EQUIVOCAL": "4172976", "INDETERMINATE ABNORMAL": "4219043",
    "INVALID": "46237613", "NONREACTIVE": "45884092", "NOT DETECTED": "9190",
    "PAST INFECTION": "36309657", "PRESUMPTIVE POSITIVE": "36715206",
    "REACTIVE": "45881802", "RECENT INFECTION": "36307621",
    "SPECIMEN UNSATISFACTORY": "36311004", "SUSPECTED": "45884696",
    "NI": "44814650", "UN": "44814653", "OT": "44814649", "": "44814653",
}

# --- OBS_CLIN "common" fan-in dicts (M3) ------------------------------------

obs_clin_type_concept_id_dict = {
    "BI": "32821", "CL": "32810", "RG": "32879", "DR": "32858", "PR": "32865",
    "PD": "45754907", "HC": "46237885", "HD": "44814653", "NI": "44814650",
    "UN": "44814653", "OT": "44814649", "": "44814653",
}

obs_clin_result_qual_value_as_concept_id_dict = {
    "POSITIVE": "9191", "NEGATIVE": "9189", "BORDERLINE": "4162852", "ELEVATED": "4328749",
    "HIGH": "4328749", "LOW": "4267416", "NORMAL": "4124457", "ABNORMAL": "4183448",
    "UNDETERMINED": "4160775", "UNDETECTABLE": "44814653", "NI": "44814650",
    "UN": "44814653", "OT": "44814649", "": "44814653",
}

obs_clin_result_modifier_qualifier_concept_id_dict = {
    "EQ": "4172703", "GE": "4171755", "GT": "4172704", "LE": "4171754", "LT": "4171756",
    "TX": "44814649", "NI": "44814650", "UN": "44814653", "OT": "44814649", "": "44814653",
}

# --- OBS_GEN "common" fan-in dicts (M3) -------------------------------------

obs_gen_type_concept_id_dict = {
    "BI": "32821", "CL": "32810", "RG": "32879", "DR": "32858", "PR": "32865",
    "PD": "45754907", "HC": "46237885", "HD": "44814653", "NI": "44814650",
    "UN": "44814653", "OT": "44814649", "": "44814653",
}

obs_gen_result_qual_value_as_concept_id_dict = {
    "POSITIVE": "9191", "NEGATIVE": "9189", "BORDERLINE": "4162852", "ELEVATED": "4328749",
    "HIGH": "4328749", "LOW": "4267416", "NORMAL": "4124457", "ABNORMAL": "4183448",
    "UNDETERMINED": "4160775", "UNDETECTABLE": "44814653", "NI": "44814650",
    "UN": "44814653", "OT": "44814649", "": "44814653",
}

obs_gen_result_modifier_qualifier_concept_id_dict = {
    "EQ": "4172703", "GE": "4171755", "GT": "4172704", "LE": "4171754", "LT": "4171756",
    "TX": "44814649", "NI": "44814650", "UN": "44814653", "OT": "44814649", "": "44814653",
}

# --- VITAL "common" fan-in dicts (M3) ---------------------------------------

vital_source_to_type_concept_id_dict = {
    "PR": "32865", "PD": "32865", "HC": "32817", "HD": "32817", "DR": "32880",
    "NI": "0", "UN": "0", "OT": "0", "": "0",
}

bp_position_diastolic_to_pb_code_dict = {
    "01": "8453-3", "02": "8454-1", "03": "8455-8", "NI": "271650006",
    "UN": "271650006", "OT": "271650006", "": "271650006",
}

bp_position_systolic_to_pb_code_dict = {
    "01": "8459-0", "02": "8460-8", "03": "8461-6", "NI": "271649006",
    "UN": "271649006", "OT": "271649006", "": "271649006",
}

bp_position_to_pb_code_type_dict = {
    "01": "LC", "02": "LC", "03": "LC", "NI": "SM", "UN": "SM", "OT": "SM", "": "SM",
}

smoking_to_smoking_concept_id_dict = {
    "01": "45881517", "02": "45884037", "03": "45883458", "04": "45879404",
    "05": "45881518", "06": "45885135", "07": "45884038", "08": "45878118",
    "NI": "0", "UN": "0", "OT": "0", "": "0",
}

smoking_to_smoking_text_dict = {
    "01": "Current every day smoker", "02": "Current some day smoker",
    "03": "Former smoker", "04": "Never smoker",
    "05": "Smoker, current status unknown", "06": "Unknown if ever smoked",
    "07": "Heavy tobacco smoker", "08": "Light tobacco smoker",
    "NI": "0", "UN": "0", "OT": "0", "": "0",
}

tobacco_to_tobacco_concept_id_dict = {
    "01": "36309332", "02": "45883537", "03": "36307819", "04": "4184633",
    "06": "45882295", "NI": "0", "UN": "0", "OT": "0", "": "0",
}

tobacco_to_tobacco_text_dict = {
    "01": "Current smoker", "02": "Never smoked", "03": "Former user",
    "04": "Passive smoker", "06": "Not asked", "NI": "No Infromation",
    "UN": "Unknown", "OT": "Other", "": "No Information",
}

tobacco_type_to_tobacco_type_concept_id_dict = {
    "01": "42530793", "02": "42531042", "03": "42531020", "04": "45878582",
    "05": "42530756", "NI": "0", "UN": "0", "OT": "0", "": "0",
}

tobacco_type_to_tobacco_type_text_dict = {
    "01": "Smoked tobacco only", "02": "Non-smoked tobacco only",
    "03": "Use of both smoked and non-smoked tobacco products", "04": "None",
    "05": "Use of smoked tobacco but no information about non-smoked tobacco use",
    "NI": "No Infromation", "UN": "Unknown", "OT": "Other", "": "No Information",
}

# --- Vocabulary_id -> PCORnet code-type abbreviation ------------------------
# Used when resolving source_concept_id from the OMOP CONCEPT view: the
# PCORnet DX_TYPE/PX_TYPE code is matched against the CONCEPT row's
# vocabulary_id collapsed to the PCORnet two-letter code.

pcornet_code_type_dict = {
    "RXNORM EXTENSION": "RX",
    "RXNORM": "RX",
    "NDC": "ND",
    "SNOMED": "SM",
    "SNOMED CT": "SM",
    "CPT4": "CH",
    "CPT": "CH",
    "CVX": "CX",
    "LOINC": "LC",
    "HCPCS": "CH",
    "ICD-9-CM": "09",
    "ICD-9": "09",
    "ICD-10-CM": "10",
    "ICD-10": "10",
    "ICD-11-CM": "11",
    "ICD10PCS": "10",
    "ICD10CM": "10",
    "ICD9Proc": "09",
}
