#!/usr/bin/env python3
"""
concepts.py
-----------
The semantic layer: Avni concept names -> analysis column names + dtypes, plus
the value-coercion helpers every other module shares.

Avni observations are EAV: ``{conceptName: value}``. Concept names are long,
human-authored questions ("What is the chlorine reading?") and they change when
someone renames a question in Admin. This module is the ONE place that knows how
a question maps to a column, so a rename is a one-line fix here.

Nothing is silently dropped: a concept with no mapping still lands in
``observations_long.csv`` (with an empty ``concept_column``) and is reported as
an unmapped concept by transform.py.

USAGE
-----
    from avni_pipeline import concepts

    spec = concepts.concept_spec("What is the chlorine reading?")
    spec.column                       # 'chlorine_residual_mg_l'
    concepts.coerce(spec, "0.05")     # 0.05  (float)
    concepts.classify_value(raw)      # {'text','number','date','is_list','media',...}
    concepts.indicator_column("General Grievance", "Other")   # 'grv_other'
"""

import ast
import datetime
import re

# --------------------------------------------------------------------------- #
# Value classification (dtype-agnostic; used by observations_long + the report)
# --------------------------------------------------------------------------- #

IMAGE_EXT_RE = re.compile(r"\.(jpe?g|png|gif|webp|bmp|tiff?)(\?|$)", re.IGNORECASE)
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
TRUEISH = {"yes", "true", "1", "y"}
FALSEISH = {"no", "false", "0", "n"}


def is_media_url(text):
    """True for the S3 media URLs Avni stores for image/file questions."""
    text = str(text).strip()
    if not (text.startswith("http://") or text.startswith("https://")):
        return False
    return bool(IMAGE_EXT_RE.search(text)) or "amazonaws" in text


def as_list(value):
    """Return value as a list of scalars, or None if it is not a list.

    Handles both the API shape (a real ``list``) and the Python literal string
    ``"['a', 'b']"`` that a predecessor script, no longer in this repository,
    printed -- text dumps in that shape still parse, so keep tolerating it.
    """
    if isinstance(value, (list, tuple)):
        return [v for v in value]
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = ast.literal_eval(s)
            except (ValueError, SyntaxError):
                return None
            if isinstance(parsed, list):
                return list(parsed)
    return None


def parse_number(value):
    """float(value) or None. Bools are NOT numbers here (they are coded answers)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_datetime(value):
    """Parse an Avni timestamp / date to an aware UTC datetime, else None."""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s or s == "-":
            return None
        s = s.replace("Z", "+00:00")
        try:
            dt = datetime.datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def parse_date(value, tzinfo=None):
    """Parse to a calendar ``date``, else None.

    A bare ``yyyy-mm-dd`` is already a calendar date and is returned as-is. A
    timestamp is converted to ``tzinfo`` first (the run's local timezone) and
    *then* truncated, so Avni's "midnight IST expressed in UTC" encoding
    (``...T18:30:00.000Z`` on the previous UTC day) lands on the India date.
    Without ``tzinfo`` the UTC date part is used.
    """
    if value is None:
        return None
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        return value
    if not isinstance(value, datetime.datetime):
        s = str(value).strip()
        if DATE_RE.match(s):
            try:
                return datetime.date.fromisoformat(s)
            except ValueError:
                return None
    dt = parse_datetime(value)
    if dt is None:
        return None
    return (dt.astimezone(tzinfo) if tzinfo is not None else dt).date()


def parse_time_minutes(value):
    """'HH:MM' -> minutes since local midnight, else None."""
    if value is None:
        return None
    m = TIME_RE.match(str(value).strip())
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def parse_bool(value):
    """Yes/No/true/false/1/0 -> bool, else None."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in TRUEISH:
        return True
    if s in FALSEISH:
        return False
    return None


def classify_value(value):
    """Describe one raw observation value without needing a concept mapping.

    Returns a dict used by observations_long.csv and by the HTML report:
    ``{'text', 'items', 'is_list', 'number', 'date', 'media'}`` where ``items``
    is the exploded list (always a list, length 1 for scalars) and ``media`` is
    the list of media URLs found.
    """
    lst = as_list(value)
    items = [("" if v is None else v) for v in lst] if lst is not None else [value]
    media = [str(v) for v in items if is_media_url(v)]
    number = parse_number(value) if lst is None else None
    date = None
    if lst is None and not media and isinstance(value, str):
        if DATE_RE.match(value.strip()) or DATETIME_RE.match(value.strip()):
            date = parse_datetime(value)
    return {
        "text": stringify(value),
        "items": items,
        "is_list": lst is not None,
        "number": number,
        "date": date,
        "media": media,
    }


def stringify(value):
    """Stable text rendering of any observation value (lists become pipe-joined)."""
    lst = as_list(value)
    if lst is not None:
        return "|".join("" if v is None else str(v) for v in lst)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def slug(text, maxlen=40):
    """'I can not open the survey' -> 'i_can_not_open_the_survey' (safe column bit)."""
    s = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")
    return (s[:maxlen].rstrip("_") or "value")


# --------------------------------------------------------------------------- #
# Concept -> column mapping
# --------------------------------------------------------------------------- #

class ConceptSpec:
    """How one Avni concept becomes one analysis column."""

    __slots__ = ("concept", "column", "dtype", "unit", "table", "description",
                 "indicator_prefix", "coalesce_source")

    def __init__(self, concept, column, dtype, table, unit="", description="",
                 indicator_prefix=None, coalesce_source=None):
        self.concept = concept
        self.column = column
        self.dtype = dtype            # see DTYPES below
        self.unit = unit
        self.table = table            # target fact table (or 'subjects')
        self.description = description
        self.indicator_prefix = indicator_prefix   # coded_multi only
        self.coalesce_source = coalesce_source     # e.g. 'v1' / 'v2' for tablets

    def __repr__(self):
        return f"<ConceptSpec {self.concept!r} -> {self.column} ({self.dtype})>"


DTYPES = ("string", "int", "float", "bool", "date", "datetime", "time",
          "media", "media_list", "coded", "coded_multi", "uuid")

SURVEY = "fact_self_report_survey"
GRIEVANCE = "fact_grievance"
PAYMENT = "fact_payment_rate"

_SPEC_LIST = [
    # ---- Self-report Survey (v1 + v2 unified) ------------------------------
    ConceptSpec("What is the chlorine reading?", "chlorine_residual_mg_l", "float",
                SURVEY, "mg/L",
                "Free chlorine residual read off the colour strip (v1 wording).",
                coalesce_source="v1"),
    # v2 renamed the question to describe the colour-match action instead of
    # naming the measurement. Same column, same mg/L units: the live values are
    # the DPD chart scale (0.05, 0.1, 0.2, 0.4, 0.6, 0.7, 1, 2, 3, 4), not a
    # shade index. Only one wording is ever present on a given encounter, so
    # these two specs never collide on the same row.
    ConceptSpec("Compare the color of the water on the tube to the right to the shades "
                "on the left hand side. Which number indicates the color with the "
                "closest match?", "chlorine_residual_mg_l", "float",
                SURVEY, "mg/L",
                "Free chlorine residual read off the colour strip (v2 wording).",
                coalesce_source="v2"),
    ConceptSpec("How many tablets did you refill?", "tablets_refilled_count", "int",
                SURVEY, "tablets", "Chlorine tablets added to the cartridge (v1 wording).",
                coalesce_source="v1"),
    ConceptSpec("Number of tablets refilled", "tablets_refilled_count", "int",
                SURVEY, "tablets", "Chlorine tablets added to the cartridge (v2 wording).",
                coalesce_source="v2"),
    ConceptSpec("How many parts of the ILC device are damaged or leaking?",
                "ilc_damaged_parts_count", "int", SURVEY, "parts",
                "Count of damaged/leaking parts on the in-line chlorinator."),
    ConceptSpec("Are there any parts of the chlorine device which are damaged or leaking?",
                "device_has_damage", "bool", SURVEY, "",
                "v2 Yes/No gate before the damaged-parts count. Null on v1 forms."),
    ConceptSpec("This week, what time did you normally turn on the water pump?",
                "pump_on_time_local", "time", SURVEY, "HH:MM local",
                "Typical pump start time this week (operator-reported, local clock)."),
    ConceptSpec("This week, what time did you normally turn off the water pump?",
                "pump_off_time_local", "time", SURVEY, "HH:MM local",
                "Typical pump stop time this week (operator-reported, local clock)."),
    ConceptSpec("When did you last refill the cartridge? (Please choose today's date "
                "if you have done the refill today)", "last_cartridge_refill_at",
                "datetime", SURVEY, "UTC", "Operator-entered last cartridge refill."),
    ConceptSpec("Please open the lid of the device, take out the cartridge and take a "
                "clear picture of it so that tablets are visible", "cartridge_photo_url",
                "media", SURVEY, "URL", "Cartridge photo (private S3 URL; needs auth)."),
    ConceptSpec("Please upload a picture of the water sample after adding the liquid, "
                "next to the colour strip", "water_sample_photo_url", "media", SURVEY,
                "URL", "Water-sample/colour-strip photo (private S3 URL)."),
    ConceptSpec("Please upload a picture of the damaged or leaking device part",
                "damaged_part_photo_urls", "media_list", SURVEY, "URL(s)",
                "Damaged-part photos, pipe-joined; count in damaged_part_photo_count."),
    ConceptSpec("Please refill new tablets in the cartridge. Take a clear picture of "
                "the new chlorine tablets in the cartridge.", "new_tablets_photo_url",
                "media", SURVEY, "URL", "v2 photo of the newly refilled tablets."),

    # ---- Grievance form ----------------------------------------------------
    ConceptSpec("Kind of Grievance", "grievance_kind", "coded", GRIEVANCE, "",
                "General vs Survey related."),
    ConceptSpec("Select the survey for which this grievance is raised",
                "grievance_target_encounter_id", "uuid", GRIEVANCE, "",
                "FK to the survey encounter the grievance is about."),
    ConceptSpec("Other greiveance", "grievance_other_text", "string", GRIEVANCE, "",
                "Free-text grievance (source concept name carries the typo)."),
    ConceptSpec("General Grievance", "general_grievance", "coded_multi", GRIEVANCE, "",
                "Multi-select general grievance reasons.", indicator_prefix="grv"),
    ConceptSpec("Survey Grievance", "survey_grievance", "coded_multi", GRIEVANCE, "",
                "Multi-select payment/survey grievance reasons.", indicator_prefix="sgrv"),

    # ---- Payment Rate ------------------------------------------------------
    ConceptSpec("Odisha Rate", "rate_odisha_inr", "float", PAYMENT, "INR",
                "Per-survey payment rate for Odisha."),
    ConceptSpec("Andhra Pradesh Rate", "rate_andhra_pradesh_inr", "float", PAYMENT,
                "INR", "Per-survey payment rate for Andhra Pradesh."),
    ConceptSpec("Effective Date", "rate_effective_date", "date", PAYMENT, "",
                "Date the rate takes effect."),

    # ---- Subject registration ---------------------------------------------
    ConceptSpec("First name", "subject_name", "string", "subjects", "",
                "The pump label; Avni stores it in the registration form."),
    ConceptSpec("Last name", "subject_last_name", "string", "subjects", "",
                "Second registration name field (usually blank for pumps)."),
]

CONCEPTS = {}
for _s in _SPEC_LIST:
    CONCEPTS[_s.concept] = _s

# Concepts whose value is a media URL (or list of them).
MEDIA_DTYPES = ("media", "media_list")


def concept_spec(concept_name):
    """ConceptSpec for a concept name, or None if it is not mapped.

    Matching is exact first, then whitespace-normalised, so a stray double space
    in the Admin-configured question does not orphan a column.
    """
    if concept_name in CONCEPTS:
        return CONCEPTS[concept_name]
    key = re.sub(r"\s+", " ", str(concept_name)).strip()
    if key in CONCEPTS:
        return CONCEPTS[key]
    for concept, spec in CONCEPTS.items():
        if re.sub(r"\s+", " ", concept).strip() == key:
            return spec
    return None


def coerce(spec, value, tzinfo=None):
    """Cast a raw observation value according to its ConceptSpec dtype.

    Returns the typed Python value (or None). Never raises: an unparseable value
    becomes None and is still visible in observations_long.csv as text.
    ``tzinfo`` is the run's local timezone; ``date`` concepts are calendar dates
    in that zone (see ``parse_date``).
    """
    if value is None or value == "":
        return None
    dt = spec.dtype
    if dt == "float":
        return parse_number(value)
    if dt == "int":
        n = parse_number(value)
        return int(round(n)) if n is not None else None
    if dt == "bool":
        return parse_bool(value)
    if dt == "date":
        return parse_date(value, tzinfo)
    if dt == "datetime":
        return parse_datetime(value)
    if dt == "time":
        return str(value).strip() if TIME_RE.match(str(value).strip()) else None
    if dt == "media":
        lst = as_list(value)
        if lst:
            return str(lst[0])
        return str(value).strip()
    if dt == "media_list":
        lst = as_list(value)
        return [str(v) for v in (lst if lst is not None else [value])]
    if dt == "coded_multi":
        lst = as_list(value)
        return [str(v).strip() for v in (lst if lst is not None else [value]) if str(v).strip()]
    # string / coded / uuid
    return stringify(value)


# --------------------------------------------------------------------------- #
# Multi-select -> one-hot indicator columns
# --------------------------------------------------------------------------- #
# Curated names for the answers we have seen (plan §5.3). Anything NOT in here
# still gets a column (prefix + slug) and is reported as an unmapped answer, so
# the map can be extended rather than silently losing a new coded answer.

INDICATOR_NAMES = {
    "General Grievance": {
        "I can not open the survey": "grv_cannot_open_survey",
        "I do not have enough chlorine tablets and hence I can not fill the survey":
            "grv_insufficient_tablets",
        "There was no water suply in the village and hence I can not fill the survey":
            "grv_no_water_supply",
        "The chlorine device is damaged and hence I can not fill the survey":
            "grv_device_damaged",
        "Other": "grv_other",
    },
    "Survey Grievance": {
        "I have received an incorrect payment amount": "sgrv_incorrect_payment",
        "I uploaded the form correctly but did not recieve the payment":
            "sgrv_uploaded_no_payment",
    },
}


def indicator_column(concept_name, answer):
    """Column name for one selected answer of a multi-select concept.

    Returns ``(column, is_curated)``. Uncurated answers get
    ``<prefix>_<slug>`` so new coded answers never vanish.
    """
    curated = INDICATOR_NAMES.get(concept_name, {})
    if answer in curated:
        return curated[answer], True
    spec = concept_spec(concept_name)
    prefix = (spec.indicator_prefix if spec and spec.indicator_prefix
              else slug(concept_name, 12))
    return f"{prefix}_{slug(answer, 32)}", False


# --------------------------------------------------------------------------- #
# Encounter types -> fact tables
# --------------------------------------------------------------------------- #
# Keyed on the lower-cased encounter type name. `form_version` distinguishes the
# two survey forms so their columns can be coalesced into one fact table.

ENCOUNTER_TYPES = {
    "self-report survey": {"table": SURVEY, "form_version": "v1"},
    "self-report survey (v2)": {"table": SURVEY, "form_version": "v2"},
    "greiveance form": {"table": GRIEVANCE, "form_version": ""},
    "grievance form": {"table": GRIEVANCE, "form_version": ""},
    "payment rate": {"table": PAYMENT, "form_version": ""},
}


def encounter_target(encounter_type):
    """(fact_table, form_version) for an encounter type, or (None, '') if unmapped."""
    info = ENCOUNTER_TYPES.get(str(encounter_type or "").strip().lower())
    if not info:
        return None, ""
    return info["table"], info["form_version"]


# --------------------------------------------------------------------------- #
# Factor levels (stable ordering for categorical columns; used by the report and
# documented in data_dictionary.csv so R/pandas can rebuild the same levels).
# --------------------------------------------------------------------------- #

FACTOR_LEVELS = {
    "status": ["completed", "planned", "cancelled"],
    "encounter_category": ["general", "program"],
    "form_version": ["v1", "v2"],
    "grievance_kind": ["General", "Survey related"],
    "tablets_refilled_source": ["v1", "v2"],
    "next_due_status": ["upcoming", "due", "overdue", "none"],
}

# Address hierarchy levels we promote to their own columns (anything else stays
# in address_raw -- e.g. the PaymentRateChart subject lives at "Account-Details").
ADDRESS_LEVELS = [
    ("Village", "village"),
    ("Gram Panchayat", "gram_panchayat"),
    ("Block", "block"),
    ("District", "district"),
    ("State", "state"),
]
