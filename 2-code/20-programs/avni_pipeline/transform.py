#!/usr/bin/env python3
"""
transform.py
------------
Raw Avni records (from pull.py: a live pull or a replayed snapshot) -> the tidy,
regression-ready table set: dimensions, per-type fact tables, the long
observation backbone, a complete data dictionary and a data-quality summary.

Design rules baked in here:
  * Voided records are tombstones -- excluded from the analysis tables but kept
    in ``*_voided`` tables with a ``voided`` column, so nothing vanishes.
  * Planned (scheduled-but-not-done) encounters stay in the encounters spine and
    are excluded from the fact tables unless ``include_planned=True``.
  * Nothing is silently dropped: an unmapped concept still lands in
    observations_long (with an empty ``concept_column``) and is counted in QC.
  * Values stay TYPED here (float/int/bool/date/datetime/list/str/None);
    emit_csv.py owns the encoding rules (booleans as 0/1, empty cell = NA, ...).
  * Column order is declared, not derived, so re-runs are byte-stable.

USAGE
-----
    from avni_pipeline import transform

    tables = transform.build_tables(raw, tz="Asia/Kolkata")
    tables["fact_self_report_survey"].rows
    tables.quality["unmapped_concepts"]
"""

import datetime
import statistics

from . import concepts

SURVEY = concepts.SURVEY
GRIEVANCE = concepts.GRIEVANCE
PAYMENT = concepts.PAYMENT

CHLORINE_PLAUSIBLE_MAX = 5.0      # mg/L; outside [0, 5] is a data-entry flag, not a filter
RUNTIME_PLAUSIBLE_MAX = 20.0      # hours/day


# --------------------------------------------------------------------------- #
# Containers
# --------------------------------------------------------------------------- #

class Table:
    """One output table: a declared column order plus a list of row dicts."""

    def __init__(self, name, columns, rows=None, grain=""):
        self.name = name
        self.columns = list(columns)
        self.rows = list(rows or [])
        self.grain = grain

    def __len__(self):
        return len(self.rows)

    def add(self, row):
        self.rows.append(row)

    def column_values(self, column):
        return [r.get(column) for r in self.rows]

    def __repr__(self):
        return f"<Table {self.name} {len(self.rows)}x{len(self.columns)}>"


class TableSet:
    """Every table of one run, plus the data dictionary, manifest meta and QC."""

    def __init__(self, meta=None):
        self.tables = {}
        self.dictionary = []          # rows: table, column, dtype, unit, source_concept, ...
        self.meta = dict(meta or {})
        self.quality = {}

    def __getitem__(self, name):
        return self.tables[name]

    def __contains__(self, name):
        return name in self.tables

    def add(self, table, dictionary_specs=None):
        self.tables[table.name] = table
        for spec in (dictionary_specs or []):
            entry = dict(spec)
            entry["table"] = table.name
            entry["example"] = _example_value(table, entry["column"])
            self.dictionary.append(entry)
        return table

    def names(self):
        return list(self.tables)

    def row_counts(self):
        return {name: len(t.rows) for name, t in self.tables.items()}


def C(column, dtype, unit="", source_concept="", description=""):
    """One data-dictionary entry (also the declaration of a column's order)."""
    return {"column": column, "dtype": dtype, "unit": unit,
            "source_concept": source_concept, "description": description}


def cols(specs):
    return [s["column"] for s in specs]


def _example_value(table, column):
    for row in table.rows:
        v = row.get(column)
        if v not in (None, "", [], {}):
            return _short(v)
    return ""


def _short(value, limit=48):
    if isinstance(value, list):
        value = "|".join(str(v) for v in value)
    text = str(value)
    return text if len(text) <= limit else text[:limit - 1] + "…"


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #

def resolve_tz(name):
    """Return (tzinfo, warning). Falls back to a fixed offset if tzdata is absent."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name), ""
    except Exception as err:                                   # pragma: no cover
        if name in ("Asia/Kolkata", "Asia/Calcutta"):
            tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30), "IST")
            return tz, f"zoneinfo unavailable ({err}); used fixed +05:30 for {name}"
        return datetime.timezone.utc, f"zoneinfo unavailable ({err}); used UTC for {name}"


def _days_between(later, earlier):
    if not later or not earlier:
        return None
    days = round((later - earlier).total_seconds() / 86400.0, 3)
    return 0.0 if days == 0 else days      # avoid a '-0' cell in the CSV


# --------------------------------------------------------------------------- #
# Column declarations (these ARE the data dictionary)
# --------------------------------------------------------------------------- #

SUBJECT_COLUMNS = [
    C("subject_id", "string(uuid)", "", "Subject.ID", "Primary key of the subject (pump)."),
    C("subject_external_id", "string", "", "Subject.External ID", "Client-side id, often blank."),
    C("subject_name", "string", "", "observations.First name", "Pump label as registered."),
    C("subject_type", "factor", "", "Subject.Subject type", "Pump / PaymentRateChart."),
    C("village", "factor", "", "location.Village", "Address level 1."),
    C("gram_panchayat", "factor", "", "location.Gram Panchayat", "Address level 2."),
    C("block", "factor", "", "location.Block", "Address level 3."),
    C("district", "factor", "", "location.District", "Address level 4."),
    C("state", "factor", "", "location.State", "Address level 5."),
    C("address_raw", "string", "", "location{}", "Full hierarchy, pipe-joined (keeps non-standard levels)."),
    C("registration_date", "date", "", "Subject.Registration date", "When the pump was registered."),
    C("subj_created_at", "datetime", "UTC", "audit.Created at", "Subject creation timestamp."),
    C("subj_last_modified_at", "datetime", "UTC", "audit.Last modified at", "Subject last-modified timestamp."),
    C("subj_created_by", "factor", "", "audit.Created by", "User who registered the pump."),
    C("n_encounters", "int", "count", "derived", "Encounters attributed to this subject (all statuses)."),
    C("n_completed", "int", "count", "derived", "Completed encounters for this subject."),
    C("first_completed_at", "datetime", "UTC", "derived", "Earliest completed encounter."),
    C("last_completed_at", "datetime", "UTC", "derived", "Latest completed encounter."),
    C("distinct_operators", "int", "count", "derived", "Distinct submitting users on this subject."),
    C("voided", "bool01", "", "Subject.Voided", "1 = tombstone; voided subjects are in subjects_voided."),
]

ENCOUNTER_COLUMNS = [
    C("encounter_id", "string(uuid)", "", "Encounter.ID", "Primary key of the encounter."),
    C("subject_id", "string(uuid)", "", "derived (client-side join)", "FK to subjects.subject_id."),
    C("subject_name", "string", "", "derived", "Denormalised pump label."),
    C("subject_type", "factor", "", "Subject.Subject type", "Pump / PaymentRateChart."),
    C("encounter_type", "factor", "", "Encounter.Encounter type", "Form name, e.g. Self-report Survey."),
    C("encounter_category", "factor", "", "derived", "general | program."),
    C("form_version", "factor", "", "derived", "v1 / v2 for the two survey forms; blank otherwise."),
    C("fact_table", "factor", "", "derived", "Which fact table this encounter feeds (blank = unmapped type)."),
    C("status", "factor", "", "derived", "completed | planned | cancelled."),
    C("scheduled_date", "date", "", "Encounter.Earliest scheduled date", "Visit scheduled date (UTC date part)."),
    C("completed_at", "datetime", "UTC", "Encounter.Encounter date time", "Submission timestamp."),
    C("completed_date", "date", "local", "derived", "Local calendar date of completed_at (see manifest.timezone)."),
    C("completed_year", "int", "local", "derived", "Local year of completion."),
    C("completed_month", "int", "local", "derived", "Local month 1-12."),
    C("completed_iso_week", "int", "local", "derived", "Local ISO week 1-53."),
    C("completed_day_of_week", "int", "local", "derived", "Local ISO weekday, 1=Mon .. 7=Sun."),
    C("completed_hour", "int", "local", "derived", "Local hour of day 0-23."),
    C("cancelled_at", "datetime", "UTC", "Encounter.Cancel datetime", "Cancellation timestamp."),
    C("submission_lag_days", "float", "days", "derived", "completed_at - scheduled date; negative = early."),
    C("is_on_time", "bool01", "", "derived", "1 if completed on/before the scheduled date + grace."),
    C("submitted_by", "factor", "", "audit.Created by", "Operator username who submitted."),
    C("last_modified_by", "factor", "", "audit.Last modified by", "User who last edited."),
    C("created_at", "datetime", "UTC", "audit.Created at", "Record creation timestamp."),
    C("last_modified_at", "datetime", "UTC", "audit.Last modified at", "Record last-modified timestamp."),
    C("program", "factor", "", "ProgramEncounter.Program", "Program name; blank for general encounters."),
    C("enrolment_id", "string(uuid)", "", "ProgramEncounter.Enrolment ID", "FK to the enrolment; blank for general."),
    C("n_observations", "int", "count", "derived", "Number of answered concepts on this encounter."),
    C("attribution", "factor", "", "derived", "How the subject link was made (subject-id | subject-doc-fallback | ...)."),
    C("voided", "bool01", "", "Encounter.Voided", "1 = tombstone; voided rows are in encounters_voided."),
]

FACT_KEY_COLUMNS = [
    C("encounter_id", "string(uuid)", "", "Encounter.ID", "PK; FK to encounters.encounter_id."),
    C("subject_id", "string(uuid)", "", "derived (client-side join)", "FK to subjects.subject_id."),
    C("subject_name", "string", "", "derived", "Denormalised pump label."),
    C("village", "factor", "", "location.Village", "Denormalised for grouping."),
    C("district", "factor", "", "location.District", "Denormalised for grouping."),
    C("state", "factor", "", "location.State", "Denormalised for grouping."),
    C("submitted_by", "factor", "", "audit.Created by", "Operator username."),
    C("completed_at", "datetime", "UTC", "Encounter.Encounter date time", "Submission timestamp."),
    C("completed_date", "date", "local", "derived", "Local calendar date of submission."),
]

SURVEY_COLUMNS = FACT_KEY_COLUMNS + [
    C("form_version", "factor", "", "derived", "v1 = Self-report Survey, v2 = Self-report survey (v2)."),
    C("chlorine_residual_mg_l", "float", "mg/L", "What is the chlorine reading?",
      "Free chlorine residual from the colour strip."),
    C("tablets_refilled_count", "int", "tablets", "How many tablets did you refill? / Number of tablets refilled",
      "Tablets added; v1 and v2 wordings coalesced."),
    C("tablets_refilled_source", "factor", "", "derived", "Which wording supplied the value: v1 | v2 | both."),
    C("ilc_damaged_parts_count", "int", "parts", "How many parts of the ILC device are damaged or leaking?",
      "Damaged/leaking parts on the in-line chlorinator."),
    C("device_has_damage", "bool01", "", "Are there any parts of the chlorine device which are damaged or leaking?",
      "v2 Yes/No gate; empty on v1 forms (use ilc_any_damage instead)."),
    C("ilc_any_damage", "bool01", "", "derived", "1 if ilc_damaged_parts_count > 0 or device_has_damage is true."),
    C("pump_on_time_local", "time", "HH:MM local", "This week, what time did you normally turn on the water pump?",
      "Typical pump start time (operator-reported local clock)."),
    C("pump_on_minutes", "int", "minutes", "derived", "pump_on_time_local as minutes since local midnight."),
    C("pump_off_time_local", "time", "HH:MM local", "This week, what time did you normally turn off the water pump?",
      "Typical pump stop time."),
    C("pump_off_minutes", "int", "minutes", "derived", "pump_off_time_local as minutes since local midnight."),
    C("pump_runtime_hours", "float", "hours", "derived", "off - on, wrapping past midnight (+24h if negative)."),
    C("last_cartridge_refill_at", "datetime", "UTC", "When did you last refill the cartridge? (...)",
      "Operator-entered last cartridge refill."),
    C("days_since_last_refill", "float", "days", "derived", "completed_at - last_cartridge_refill_at."),
    C("cartridge_photo_url", "string(url)", "URL", "Please open the lid ... cartridge ...",
      "Private S3 URL; needs auth to open (see README media notes)."),
    C("water_sample_photo_url", "string(url)", "URL", "Please upload a picture of the water sample ...",
      "Private S3 URL."),
    C("new_tablets_photo_url", "string(url)", "URL", "Please refill new tablets in the cartridge ...",
      "Private S3 URL (v2 only)."),
    C("damaged_part_photo_urls", "string(url list)", "URL(s)", "Please upload a picture of the damaged or leaking device part",
      "Pipe-joined private S3 URLs."),
    C("damaged_part_photo_count", "int", "count", "derived", "Number of damaged-part photos uploaded."),
    C("scheduled_date", "date", "", "Encounter.Earliest scheduled date", "Scheduled visit date."),
    C("submission_lag_days", "float", "days", "derived", "completed_at - scheduled date; negative = early."),
    C("is_on_time", "bool01", "", "derived", "1 if submitted on/before schedule + grace."),
    C("pump_submission_seq", "int", "count", "derived", "1-based survey sequence within this pump."),
    C("pump_prior_submissions", "int", "count", "derived", "Surveys this pump had before this one."),
    C("pump_days_since_prev_submission", "float", "days", "derived", "Gap to this pump's previous survey."),
    C("operator_submission_seq", "int", "count", "derived", "1-based survey sequence for this operator."),
    C("operator_days_since_prev_submission", "float", "days", "derived", "Gap to this operator's previous survey."),
    C("chlorine_out_of_range_flag", "bool01", "", "derived", "1 if chlorine outside [0, 5] mg/L (kept, not dropped)."),
    C("pump_runtime_implausible_flag", "bool01", "", "derived", "1 if runtime <= 0 or > 20 h."),
    C("refill_in_future_flag", "bool01", "", "derived", "1 if the reported refill is after the submission time."),
]

GRIEVANCE_BASE_COLUMNS = FACT_KEY_COLUMNS + [
    C("grievance_kind", "factor", "", "Kind of Grievance", "General | Survey related."),
    C("grievance_target_encounter_id", "string(uuid)", "", "Select the survey for which this grievance is raised",
      "FK to the survey encounter the grievance is about."),
    C("grievance_target_found", "bool01", "", "derived", "1 if the target encounter exists in this export."),
    C("grievance_other_text", "string", "", "Other greiveance", "Free text (source concept carries the typo)."),
    C("general_grievance_raw", "string", "", "General Grievance", "Selected answers, pipe-joined."),
    C("survey_grievance_raw", "string", "", "Survey Grievance", "Selected answers, pipe-joined."),
    C("n_grievance_reasons", "int", "count", "derived", "Total selected answers across both multi-selects."),
]

PAYMENT_COLUMNS = FACT_KEY_COLUMNS + [
    C("rate_odisha_inr", "float", "INR", "Odisha Rate", "Per-survey payment rate for Odisha."),
    C("rate_andhra_pradesh_inr", "float", "INR", "Andhra Pradesh Rate", "Per-survey payment rate for Andhra Pradesh."),
    C("rate_effective_date", "date", "", "Effective Date", "Date the rate takes effect."),
]

LONG_COLUMNS = [
    C("subject_id", "string(uuid)", "", "derived", "FK to subjects.subject_id."),
    C("encounter_id", "string(uuid)", "", "Encounter.ID", "FK to encounters.encounter_id."),
    C("encounter_type", "factor", "", "Encounter.Encounter type", "Form name."),
    C("concept_name", "string", "", "observations key", "The Avni concept (question) name, verbatim."),
    C("concept_column", "string", "", "concepts.py", "Mapped analysis column; empty = unmapped concept."),
    C("value_text", "string", "", "observations value", "Value as text (one row per selected answer)."),
    C("value_num", "float", "", "derived", "Numeric parse of value_text when it is a number."),
    C("value_date", "datetime", "UTC", "derived", "Datetime parse of value_text when it is a date/time."),
    C("is_list", "bool01", "", "derived", "1 if the source value was a multi-select list."),
    C("list_index", "int", "", "derived", "0-based position within the multi-select; empty for scalars."),
    C("media_url", "string(url)", "URL", "derived", "Set when the value is a media URL (private S3)."),
    C("submitted_by", "factor", "", "audit.Created by", "Operator username."),
    C("completed_at", "datetime", "UTC", "Encounter.Encounter date time", "Submission timestamp."),
]

OPERATOR_COLUMNS = [
    C("operator", "string", "", "audit.Created by", "Avni username (PK)."),
    C("n_submissions", "int", "count", "derived", "Completed encounters submitted."),
    C("n_surveys", "int", "count", "derived", "Self-report surveys submitted."),
    C("n_grievances", "int", "count", "derived", "Grievance forms submitted."),
    C("n_payment_rates", "int", "count", "derived", "Payment-rate forms submitted."),
    C("n_other", "int", "count", "derived", "Submissions of unmapped encounter types."),
    C("distinct_subjects", "int", "count", "derived", "Distinct pumps this operator submitted for."),
    C("first_submission_at", "datetime", "UTC", "derived", "Earliest submission."),
    C("last_submission_at", "datetime", "UTC", "derived", "Latest submission."),
    C("active_days", "int", "days", "derived", "Distinct local dates with a submission."),
    C("span_days", "float", "days", "derived", "last - first submission."),
    C("mean_gap_days", "float", "days", "derived", "Mean gap between consecutive submissions."),
    C("gap_cv", "float", "ratio", "derived", "Coefficient of variation of gaps = consistency score (lower = steadier)."),
    C("on_time_rate", "float", "ratio", "derived", "Share of scheduled submissions made on time."),
]


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #

def build_tables(raw, tz="Asia/Kolkata", include_planned=False, onehot=True,
                 grace_days=0, chlorine_band=None):
    """Turn a raw dataset into the full TableSet.

    ``tz`` drives every local calendar field; ``include_planned`` keeps
    scheduled-only encounters in the fact tables; ``onehot`` toggles multi-select
    indicator columns; ``grace_days`` widens the on-time window; ``chlorine_band``
    is an optional ``(low, high)`` that adds ``chlorine_in_target``.
    """
    tzinfo, tz_warning = resolve_tz(tz)
    ts = TableSet(meta=dict(raw.get("meta") or {}))
    ts.meta["timezone"] = tz
    ts.meta["include_planned"] = bool(include_planned)
    ts.meta["onehot_multiselect"] = bool(onehot)
    ts.meta["on_time_grace_days"] = grace_days
    ts.meta["chlorine_band"] = list(chlorine_band) if chlorine_band else None

    qc = {"attribution": dict(raw.get("attribution") or {}),
          "unmapped_concepts": {}, "unmapped_multiselect_answers": {},
          "unmapped_encounter_types": {}, "warnings": []}
    if tz_warning:
        qc["warnings"].append(tz_warning)

    all_encounters = list(raw.get("encounters") or []) + list(raw.get("program_encounters") or [])

    # ---- subjects ---------------------------------------------------------
    subject_rows, subject_by_id = _build_subjects(raw.get("subjects") or [])

    # ---- encounters spine -------------------------------------------------
    enc_rows = _build_encounters(all_encounters, subject_by_id, tzinfo, grace_days, qc)

    live_enc = [r for r in enc_rows if not r["voided"]]
    voided_enc = [r for r in enc_rows if r["voided"]]
    enc_by_id = {r["encounter_id"]: r for r in live_enc}

    # per-subject rollups now that encounters are known
    _rollup_subjects(subject_rows, live_enc)
    live_subjects = [r for r in subject_rows if not r["voided"]]
    voided_subjects = [r for r in subject_rows if r["voided"]]

    ts.add(Table("subjects", cols(SUBJECT_COLUMNS), live_subjects,
                 grain="1 row per subject (pump)"), SUBJECT_COLUMNS)
    ts.add(Table("encounters", cols(ENCOUNTER_COLUMNS), live_enc,
                 grain="1 row per encounter (all types)"), ENCOUNTER_COLUMNS)

    # ---- observations_long (the tidy backbone) ----------------------------
    long_rows = _build_long(all_encounters, enc_by_id, qc)
    ts.add(Table("observations_long", cols(LONG_COLUMNS), long_rows,
                 grain="1 row per (encounter, concept, selected answer)"), LONG_COLUMNS)

    # ---- facts ------------------------------------------------------------
    obs_by_encounter = {e.get("ID"): (e.get("observations") or {}) for e in all_encounters}
    fact_source = [r for r in live_enc
                   if include_planned or r["status"] == "completed"]
    qc["planned_or_cancelled_excluded_from_facts"] = (
        0 if include_planned else sum(1 for r in live_enc if r["status"] != "completed"))

    survey_specs, survey_rows = _build_survey_fact(
        fact_source, obs_by_encounter, subject_by_id, chlorine_band, qc)
    ts.add(Table(SURVEY, cols(survey_specs), survey_rows,
                 grain="1 row per self-report survey"), survey_specs)

    survey_ids = {r["encounter_id"] for r in survey_rows}
    grievance_specs, grievance_rows = _build_grievance_fact(
        fact_source, obs_by_encounter, subject_by_id, survey_ids, onehot, qc)
    ts.add(Table(GRIEVANCE, cols(grievance_specs), grievance_rows,
                 grain="1 row per grievance form"), grievance_specs)

    payment_rows = _build_payment_fact(fact_source, obs_by_encounter, subject_by_id)
    ts.add(Table(PAYMENT, cols(PAYMENT_COLUMNS), payment_rows,
                 grain="1 row per payment-rate change"), PAYMENT_COLUMNS)

    # ---- operators --------------------------------------------------------
    operator_rows = _build_operators(live_enc, tzinfo)
    ts.add(Table("operators", cols(OPERATOR_COLUMNS), operator_rows,
                 grain="1 row per submitting user"), OPERATOR_COLUMNS)

    # ---- tombstones -------------------------------------------------------
    ts.add(Table("subjects_voided", cols(SUBJECT_COLUMNS), voided_subjects,
                 grain="voided subjects (audit trail)"))
    ts.add(Table("encounters_voided", cols(ENCOUNTER_COLUMNS), voided_enc,
                 grain="voided encounters (audit trail)"))

    ts.quality = _quality(ts, qc, raw)
    return ts


# --------------------------------------------------------------------------- #
# Subjects
# --------------------------------------------------------------------------- #

def _subject_name(subject):
    obs = subject.get("observations") or {}
    person = " ".join(str(x) for x in (obs.get("First name"), obs.get("Last name")) if x)
    return (subject.get("Name") or obs.get("Name") or person.strip()
            or subject.get("_tree_name") or subject.get("ID") or "(unnamed)")


def _address(location):
    """(levels dict, raw string). Drops '* External ID' keys and empty levels."""
    levels, parts = {}, []
    location = location or {}
    for label, column in concepts.ADDRESS_LEVELS:
        value = location.get(label)
        levels[column] = value if value not in ("", None, "None") else None
    for key, value in location.items():
        if key.endswith("External ID") or value in ("", None, "None"):
            continue
        parts.append(f"{key}={value}")
    return levels, "|".join(parts)


def _build_subjects(subjects):
    rows, by_id = [], {}
    for s in subjects:
        audit = s.get("audit") or {}
        levels, address_raw = _address(s.get("location"))
        row = {
            "subject_id": s.get("ID"),
            "subject_external_id": s.get("External ID") or None,
            "subject_name": _subject_name(s),
            "subject_type": s.get("Subject type"),
            "address_raw": address_raw,
            "registration_date": concepts.parse_date(s.get("Registration date")),
            "subj_created_at": concepts.parse_datetime(audit.get("Created at")),
            "subj_last_modified_at": concepts.parse_datetime(audit.get("Last modified at")),
            "subj_created_by": audit.get("Created by") or None,
            "n_encounters": 0, "n_completed": 0,
            "first_completed_at": None, "last_completed_at": None,
            "distinct_operators": 0,
            "voided": bool(s.get("Voided")),
        }
        row.update(levels)
        rows.append(row)
        by_id[row["subject_id"]] = row
    rows.sort(key=lambda r: (str(r["subject_type"] or ""), str(r["subject_name"] or "")))
    return rows, by_id


def _rollup_subjects(subject_rows, enc_rows):
    by_subject = {}
    for e in enc_rows:
        by_subject.setdefault(e["subject_id"], []).append(e)
    for row in subject_rows:
        mine = by_subject.get(row["subject_id"], [])
        done = [e for e in mine if e["status"] == "completed" and e["completed_at"]]
        row["n_encounters"] = len(mine)
        row["n_completed"] = len(done)
        stamps = sorted(e["completed_at"] for e in done)
        row["first_completed_at"] = stamps[0] if stamps else None
        row["last_completed_at"] = stamps[-1] if stamps else None
        row["distinct_operators"] = len({e["submitted_by"] for e in mine if e["submitted_by"]})


# --------------------------------------------------------------------------- #
# Encounters
# --------------------------------------------------------------------------- #

def _status(encounter):
    if encounter.get("Cancel datetime"):
        return "cancelled"
    if encounter.get("Encounter date time"):
        return "completed"
    return "planned"


def _build_encounters(encounters, subject_by_id, tzinfo, grace_days, qc):
    rows = []
    for e in encounters:
        etype = e.get("Encounter type")
        fact_table, form_version = concepts.encounter_target(etype)
        if fact_table is None:
            qc["unmapped_encounter_types"][str(etype)] = \
                qc["unmapped_encounter_types"].get(str(etype), 0) + 1

        subject_id = e.get("_subject_id") or e.get("Subject ID")
        subject = subject_by_id.get(subject_id, {})
        audit = e.get("audit") or {}
        completed = concepts.parse_datetime(e.get("Encounter date time"))
        scheduled = concepts.parse_datetime(e.get("Earliest scheduled date"))
        local = completed.astimezone(tzinfo) if completed else None
        lag = _days_between(completed, scheduled)
        rows.append({
            "encounter_id": e.get("ID"),
            "subject_id": subject_id,
            "subject_name": subject.get("subject_name"),
            "subject_type": subject.get("subject_type") or e.get("Subject type"),
            "encounter_type": etype,
            "encounter_category": e.get("_category") or ("program" if e.get("Enrolment ID") else "general"),
            "form_version": form_version,
            "fact_table": fact_table or "",
            "status": _status(e),
            "scheduled_date": scheduled.date() if scheduled else None,
            "completed_at": completed,
            "completed_date": local.date() if local else None,
            "completed_year": local.year if local else None,
            "completed_month": local.month if local else None,
            "completed_iso_week": local.isocalendar()[1] if local else None,
            "completed_day_of_week": local.isoweekday() if local else None,
            "completed_hour": local.hour if local else None,
            "cancelled_at": concepts.parse_datetime(e.get("Cancel datetime")),
            "submission_lag_days": lag,
            "is_on_time": (None if lag is None else lag <= float(grace_days)),
            "submitted_by": audit.get("Created by") or None,
            "last_modified_by": audit.get("Last modified by") or None,
            "created_at": concepts.parse_datetime(audit.get("Created at")),
            "last_modified_at": concepts.parse_datetime(audit.get("Last modified at")),
            "program": e.get("Program") or None,
            "enrolment_id": e.get("Enrolment ID") or e.get("_enrolment_id") or None,
            "n_observations": len(e.get("observations") or {}),
            "attribution": e.get("_attribution") or "",
            "voided": bool(e.get("Voided")),
        })
    rows.sort(key=lambda r: (str(r["subject_name"] or ""),
                             r["completed_at"].isoformat() if r["completed_at"] else "",
                             str(r["encounter_id"])))
    return rows


# --------------------------------------------------------------------------- #
# observations_long
# --------------------------------------------------------------------------- #

def _build_long(encounters, enc_by_id, qc):
    rows = []
    for e in encounters:
        spine = enc_by_id.get(e.get("ID"))
        if spine is None:          # voided encounter, or one we could not attribute
            continue
        for concept_name, value in (e.get("observations") or {}).items():
            spec = concepts.concept_spec(concept_name)
            if spec is None:
                qc["unmapped_concepts"][concept_name] = \
                    qc["unmapped_concepts"].get(concept_name, 0) + 1
            info = concepts.classify_value(value)
            items = info["items"] if info["is_list"] else [value]
            for idx, item in enumerate(items):
                text = concepts.stringify(item)
                rows.append({
                    "subject_id": spine["subject_id"],
                    "encounter_id": spine["encounter_id"],
                    "encounter_type": spine["encounter_type"],
                    "concept_name": concept_name,
                    "concept_column": spec.column if spec else "",
                    "value_text": text,
                    "value_num": concepts.parse_number(item),
                    "value_date": (concepts.parse_datetime(text)
                                   if concepts.DATE_RE.match(text) or concepts.DATETIME_RE.match(text)
                                   else None),
                    "is_list": info["is_list"],
                    "list_index": idx if info["is_list"] else None,
                    "media_url": text if concepts.is_media_url(text) else None,
                    "submitted_by": spine["submitted_by"],
                    "completed_at": spine["completed_at"],
                })
    rows.sort(key=lambda r: (str(r["encounter_id"]), r["concept_name"],
                             r["list_index"] if r["list_index"] is not None else -1))
    return rows


# --------------------------------------------------------------------------- #
# Facts
# --------------------------------------------------------------------------- #

def _fact_keys(enc_row, subject_row=None):
    return {
        "encounter_id": enc_row["encounter_id"],
        "subject_id": enc_row["subject_id"],
        "subject_name": enc_row["subject_name"],
        "village": (subject_row or {}).get("village"),
        "district": (subject_row or {}).get("district"),
        "state": (subject_row or {}).get("state"),
        "submitted_by": enc_row["submitted_by"],
        "completed_at": enc_row["completed_at"],
        "completed_date": enc_row["completed_date"],
    }


def _build_survey_fact(enc_rows, obs_by_encounter, subject_by_id, chlorine_band, qc):
    specs = list(SURVEY_COLUMNS)
    if chlorine_band:
        specs = specs + [C("chlorine_in_target", "bool01", "",
                           "derived", f"1 if chlorine within [{chlorine_band[0]}, "
                                      f"{chlorine_band[1]}] mg/L (configured band).")]
    rows = []
    for enc in enc_rows:
        if enc["fact_table"] != SURVEY:
            continue
        obs = obs_by_encounter.get(enc["encounter_id"]) or {}
        row = {c["column"]: None for c in specs}
        row.update(_fact_keys(enc, subject_by_id.get(enc["subject_id"])))
        row["form_version"] = enc["form_version"]
        row["scheduled_date"] = enc["scheduled_date"]
        row["submission_lag_days"] = enc["submission_lag_days"]
        row["is_on_time"] = enc["is_on_time"]

        v1_tablets = v2_tablets = None
        for concept_name, value in obs.items():
            spec = concepts.concept_spec(concept_name)
            if spec is None or spec.table != SURVEY:
                continue
            typed = concepts.coerce(spec, value)
            if spec.column == "tablets_refilled_count":
                if spec.coalesce_source == "v2":
                    v2_tablets = typed
                else:
                    v1_tablets = typed
                continue
            row[spec.column] = typed

        if v1_tablets is not None or v2_tablets is not None:
            row["tablets_refilled_count"] = v2_tablets if v2_tablets is not None else v1_tablets
            row["tablets_refilled_source"] = ("both" if (v1_tablets is not None and v2_tablets is not None)
                                              else ("v2" if v2_tablets is not None else "v1"))
            if (v1_tablets is not None and v2_tablets is not None
                    and v1_tablets != v2_tablets):
                qc.setdefault("tablet_field_conflicts", []).append(enc["encounter_id"])

        photos = row.get("damaged_part_photo_urls") or []
        photos = [p for p in photos if p]
        row["damaged_part_photo_urls"] = photos
        row["damaged_part_photo_count"] = len(photos)

        row["pump_on_minutes"] = concepts.parse_time_minutes(row.get("pump_on_time_local"))
        row["pump_off_minutes"] = concepts.parse_time_minutes(row.get("pump_off_time_local"))
        if row["pump_on_minutes"] is not None and row["pump_off_minutes"] is not None:
            delta = row["pump_off_minutes"] - row["pump_on_minutes"]
            if delta < 0:
                delta += 24 * 60                       # pump ran past midnight
            row["pump_runtime_hours"] = round(delta / 60.0, 3)

        damaged = row.get("ilc_damaged_parts_count")
        row["ilc_any_damage"] = (True if (damaged or 0) > 0
                                 else (row.get("device_has_damage")
                                       if row.get("device_has_damage") is not None else False))
        row["days_since_last_refill"] = _days_between(row["completed_at"],
                                                      row.get("last_cartridge_refill_at"))

        chlorine = row.get("chlorine_residual_mg_l")
        row["chlorine_out_of_range_flag"] = (
            None if chlorine is None else (chlorine < 0 or chlorine > CHLORINE_PLAUSIBLE_MAX))
        if chlorine_band:
            row["chlorine_in_target"] = (
                None if chlorine is None
                else (chlorine_band[0] <= chlorine <= chlorine_band[1]))
        runtime = row.get("pump_runtime_hours")
        row["pump_runtime_implausible_flag"] = (
            None if runtime is None else (runtime <= 0 or runtime > RUNTIME_PLAUSIBLE_MAX))
        refill = row.get("last_cartridge_refill_at")
        row["refill_in_future_flag"] = (
            None if (refill is None or row["completed_at"] is None)
            else refill > row["completed_at"])
        rows.append(row)

    rows.sort(key=lambda r: (r["completed_at"] or datetime.datetime.min.replace(
        tzinfo=datetime.timezone.utc), str(r["encounter_id"])))
    _add_cadence(rows, "subject_id", "pump_submission_seq",
                 "pump_days_since_prev_submission", "pump_prior_submissions")
    _add_cadence(rows, "submitted_by", "operator_submission_seq",
                 "operator_days_since_prev_submission")
    return specs, rows


def _add_cadence(rows, group_key, seq_col, gap_col, prior_col=None):
    """Sequence + gap-to-previous within a group, over rows already time-sorted."""
    seen = {}
    for row in rows:
        key = row.get(group_key)
        if key is None:
            continue
        prev = seen.get(key)
        seq = (prev[0] if prev else 0) + 1
        row[seq_col] = seq
        if prior_col:
            row[prior_col] = seq - 1
        if prev and prev[1] and row["completed_at"]:
            row[gap_col] = _days_between(row["completed_at"], prev[1])
        seen[key] = (seq, row["completed_at"])


def _build_grievance_fact(enc_rows, obs_by_encounter, subject_by_id, survey_ids, onehot, qc):
    # Stable schema: every curated indicator column exists whether or not it was
    # selected in this export; uncurated answers append (sorted) and are flagged.
    indicator_specs, indicator_order = [], []
    if onehot:
        for concept_name, mapping in concepts.INDICATOR_NAMES.items():
            for answer, column in mapping.items():
                indicator_specs.append(C(column, "bool01", "", f"{concept_name} = {answer}",
                                         f"1 if '{answer}' was selected."))
                indicator_order.append(column)

    rows, extra_columns = [], {}
    for enc in enc_rows:
        if enc["fact_table"] != GRIEVANCE:
            continue
        obs = obs_by_encounter.get(enc["encounter_id"]) or {}
        row = {c["column"]: None for c in GRIEVANCE_BASE_COLUMNS}
        row.update(_fact_keys(enc, subject_by_id.get(enc["subject_id"])))
        for column in indicator_order:
            row[column] = False
        n_reasons = 0
        for concept_name, value in obs.items():
            spec = concepts.concept_spec(concept_name)
            if spec is None or spec.table != GRIEVANCE:
                continue
            typed = concepts.coerce(spec, value)
            if spec.dtype == "coded_multi":
                answers = typed or []
                n_reasons += len(answers)
                row[f"{spec.column}_raw"] = "|".join(answers)
                for answer in answers:
                    column, curated = concepts.indicator_column(concept_name, answer)
                    if not curated:
                        qc["unmapped_multiselect_answers"].setdefault(concept_name, [])
                        if answer not in qc["unmapped_multiselect_answers"][concept_name]:
                            qc["unmapped_multiselect_answers"][concept_name].append(answer)
                    if onehot:
                        if column not in row:
                            extra_columns[column] = (concept_name, answer)
                        row[column] = True
            else:
                row[spec.column] = typed
        row["n_grievance_reasons"] = n_reasons
        target = row.get("grievance_target_encounter_id")
        row["grievance_target_found"] = (None if not target else target in survey_ids)
        rows.append(row)

    for column in sorted(extra_columns):
        concept_name, answer = extra_columns[column]
        indicator_specs.append(C(column, "bool01", "", f"{concept_name} = {answer}",
                                 f"1 if '{answer}' was selected (answer not in the curated map)."))
        indicator_order.append(column)
    for row in rows:                       # backfill late-discovered columns
        for column in indicator_order:
            row.setdefault(column, False)

    rows.sort(key=lambda r: (r["completed_at"] or datetime.datetime.min.replace(
        tzinfo=datetime.timezone.utc), str(r["encounter_id"])))
    return GRIEVANCE_BASE_COLUMNS + indicator_specs, rows


def _build_payment_fact(enc_rows, obs_by_encounter, subject_by_id):
    rows = []
    for enc in enc_rows:
        if enc["fact_table"] != PAYMENT:
            continue
        obs = obs_by_encounter.get(enc["encounter_id"]) or {}
        row = {c["column"]: None for c in PAYMENT_COLUMNS}
        row.update(_fact_keys(enc, subject_by_id.get(enc["subject_id"])))
        for concept_name, value in obs.items():
            spec = concepts.concept_spec(concept_name)
            if spec is not None and spec.table == PAYMENT:
                row[spec.column] = concepts.coerce(spec, value)
        rows.append(row)
    rows.sort(key=lambda r: (r["completed_at"] or datetime.datetime.min.replace(
        tzinfo=datetime.timezone.utc), str(r["encounter_id"])))
    return rows


# --------------------------------------------------------------------------- #
# Operators
# --------------------------------------------------------------------------- #

def _build_operators(enc_rows, tzinfo):
    by_operator = {}
    for e in enc_rows:
        if not e["submitted_by"] or e["status"] != "completed":
            continue
        by_operator.setdefault(e["submitted_by"], []).append(e)

    rows = []
    for operator, mine in by_operator.items():
        stamps = sorted(e["completed_at"] for e in mine if e["completed_at"])
        gaps = [_days_between(b, a) for a, b in zip(stamps, stamps[1:])]
        gaps = [g for g in gaps if g is not None]
        mean_gap = round(statistics.fmean(gaps), 3) if gaps else None
        gap_cv = (round(statistics.stdev(gaps) / mean_gap, 3)
                  if len(gaps) > 1 and mean_gap else None)
        scheduled = [e for e in mine if e["is_on_time"] is not None]
        rows.append({
            "operator": operator,
            "n_submissions": len(mine),
            "n_surveys": sum(1 for e in mine if e["fact_table"] == SURVEY),
            "n_grievances": sum(1 for e in mine if e["fact_table"] == GRIEVANCE),
            "n_payment_rates": sum(1 for e in mine if e["fact_table"] == PAYMENT),
            "n_other": sum(1 for e in mine if not e["fact_table"]),
            "distinct_subjects": len({e["subject_id"] for e in mine if e["subject_id"]}),
            "first_submission_at": stamps[0] if stamps else None,
            "last_submission_at": stamps[-1] if stamps else None,
            "active_days": len({e["completed_date"] for e in mine if e["completed_date"]}),
            "span_days": _days_between(stamps[-1], stamps[0]) if len(stamps) > 1 else 0.0,
            "mean_gap_days": mean_gap,
            "gap_cv": gap_cv,
            "on_time_rate": (round(sum(1 for e in scheduled if e["is_on_time"]) / len(scheduled), 3)
                             if scheduled else None),
        })
    rows.sort(key=lambda r: (-r["n_submissions"], r["operator"]))
    return rows


# --------------------------------------------------------------------------- #
# Quality
# --------------------------------------------------------------------------- #

def _quality(ts, qc, raw):
    encounters = ts["encounters"].rows
    surveys = ts[SURVEY].rows
    grievances = ts[GRIEVANCE].rows

    seen, duplicates = set(), []
    for e in encounters:
        if e["encounter_id"] in seen:
            duplicates.append(e["encounter_id"])
        seen.add(e["encounter_id"])

    operators = sorted({e["submitted_by"] for e in encounters if e["submitted_by"]})
    same_day = {}
    for s in surveys:
        key = (s["subject_id"], s["completed_date"])
        same_day[key] = same_day.get(key, 0) + 1

    def flagged(column):
        return sum(1 for s in surveys if s.get(column) is True)

    quality = dict(qc)
    quality.update({
        "row_counts": ts.row_counts(),
        "duplicate_encounter_ids": duplicates,
        "encounters_without_subject": sum(1 for e in encounters if not e["subject_id"]),
        "encounters_by_status": _counter(e["status"] for e in encounters),
        "encounters_by_type": _counter(e["encounter_type"] for e in encounters),
        "distinct_operators": len(operators),
        "operators": operators,
        "orphan_grievance_targets": sum(1 for g in grievances
                                        if g["grievance_target_encounter_id"]
                                        and not g["grievance_target_found"]),
        "grievances_without_target": sum(1 for g in grievances
                                         if not g["grievance_target_encounter_id"]),
        "same_pump_same_day_surveys": sum(n - 1 for n in same_day.values() if n > 1),
        "surveys_missing_chlorine": sum(1 for s in surveys
                                        if s["chlorine_residual_mg_l"] is None),
        "flags": {
            "chlorine_out_of_range": flagged("chlorine_out_of_range_flag"),
            "pump_runtime_implausible": flagged("pump_runtime_implausible_flag"),
            "refill_in_future": flagged("refill_in_future_flag"),
            "negative_days_since_refill": sum(
                1 for s in surveys if (s["days_since_last_refill"] or 0) < 0),
        },
        "voided": {"subjects": len(ts["subjects_voided"].rows),
                   "encounters": len(ts["encounters_voided"].rows)},
    })
    if len(operators) <= 1:
        quality["warnings"].append(
            "Only one distinct submitting user: operators appear to share a login, "
            "which blocks the per-operator incentive analysis.")
    if quality["unmapped_concepts"]:
        quality["warnings"].append(
            f"{len(quality['unmapped_concepts'])} concept(s) have no column mapping "
            "(kept in observations_long; add them to concepts.py).")
    if quality["unmapped_encounter_types"]:
        quality["warnings"].append(
            f"{len(quality['unmapped_encounter_types'])} encounter type(s) feed no fact "
            "table (they are still in encounters.csv).")
    return quality


def _counter(values):
    out = {}
    for v in values:
        key = "" if v is None else str(v)
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))
