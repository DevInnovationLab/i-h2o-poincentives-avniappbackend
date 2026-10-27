# Data dictionary — Avni output tables

The eight analysis tables written by
`2-code/21-wrangling/avni_to_tables.py` into an output directory (and published
to Box `12-avni/121-clean/`). This file is the hand-written, PII-free schema
reference: it is derived from the column declarations in
`2-code/20-programs/avni_pipeline/transform.py` and the concept map in
`2-code/20-programs/avni_pipeline/concepts.py`, which are the code that actually
emits the columns.

> **The authoritative, run-specific dictionary is `data_dictionary.csv`, inside
> the output directory (Box `12-avni/121-clean/`) — not this file.** Every run
> regenerates it with one row per column of every table, including the one-hot
> indicator columns that only exist when the matching answer was selected. It is
> not published here because it carries an `example` column populated from live
> field rows, which is real study data. **This file therefore contains no example
> values.** When the two disagree, `data_dictionary.csv` for that run wins.

---

## Contents

- [Encoding contract](#encoding-contract)
- [How the tables fit together](#how-the-tables-fit-together)
- [Shared fact keys](#shared-fact-keys)
- [`subjects`](#subjects)
- [`encounters`](#encounters)
- [`observations_long`](#observations_long)
- [`fact_self_report_survey`](#fact_self_report_survey)
- [`fact_grievance`](#fact_grievance)
- [`fact_payment_rate`](#fact_payment_rate)
- [`operators`](#operators)
- [`pump_schedule`](#pump_schedule)
- [Sidecar files](#sidecar-files)

---

## Encoding contract

Every table obeys the same rules. They are implemented once, in
`2-code/20-programs/avni_pipeline/emit_csv.py`, and restated in
`data_dictionary.csv`.

| Rule | Detail |
|---|---|
| **Booleans** | `1` / `0` integers, never `TRUE`/`FALSE`. The dtype is written as `bool01`. Models read them as numerics. |
| **Missing** | An **empty cell**. Never a sentinel like `-1` or `0`, which a model would read as data. An empty `bool01` means "not asked / not applicable", which is not the same as `0`. |
| **Datetimes** | ISO 8601 in **UTC**, seconds precision, with a trailing `Z`. |
| **Dates** | ISO 8601 `yyyy-mm-dd`, and **always the local (India) calendar date** in the run's timezone — never the UTC date part. Avni sends schedule dates as local midnight expressed in UTC (an India date arrives as `18:30Z` on the *previous* UTC day); the pipeline converts such timestamps to the run timezone before taking the date. A source value that is already a bare `yyyy-mm-dd` is a calendar date and is kept as-is. Every `date` column is therefore marked *local* below. |
| **Local calendar fields** | Columns marked *local* (every `date`, plus `completed_hour`, `completed_iso_week`, …) are computed in the run's timezone, recorded as `timezone` in `_manifest.json` (default `Asia/Kolkata`, set with `--tz`). Datetimes stay UTC. Never mix the two in arithmetic. |
| **Times of day** | `HH:MM` strings as the operator entered them (local clock), each paired with a `*_minutes` integer (minutes since local midnight) for modelling. |
| **Lists** | Pipe-joined (`\|`) in a single cell. |
| **IDs** | Always strings (uuids), never coerced to numbers. |
| **Units** | In the column name: `_mg_l`, `_inr`, `_count`, `_minutes`, `_days`, `_hours`. |
| **Row and column order** | Stable and declared, not derived, so re-runs diff cleanly. |
| **Voided records** | Excluded from the analysis tables and written to `_voided/*.csv` with a `voided` column. Nothing is deleted silently. |
| **Planned encounters** | Scheduled-but-not-submitted encounters stay in `encounters` with `status = planned` and are excluded from the fact tables unless the run used `--include-planned`. |
| **Flags, not filters** | Implausible values are kept and marked with a `*_flag` boolean. The pipeline never drops a row for being odd. |

**dtypes used below**: `string`, `string(uuid)`, `string(url)`,
`string(url list)` (pipe-joined), `int`, `float`,
`bool01`, `date` (local calendar date), `datetime` (UTC unless noted), `time` (`HH:MM` local),
`factor` (a string with a small, stable set of values).

---

## How the tables fit together

A star schema. `subjects` and `operators` are dimensions, `encounters` is the
spine, the three `fact_*` tables are one per encounter type, and
`observations_long` is the schema-agnostic backbone everything else is derived
from. `pump_schedule` is a per-pump rollup of the survey encounters in
`encounters` (planned ones included), keyed by `subject_id`.

```
subjects (1 row / pump) <--subject_id-- pump_schedule (1 row / pump; survey schedule as of the pull)
   ^                                           |
   | subject_id                                +--next_due_encounter_id--> a planned survey in encounters
   |
encounters (1 row / encounter, every type)
   ^                     ^
   | encounter_id        | submitted_by -> operators.operator
   |
   +-- fact_self_report_survey   (1 row / survey)
   +-- fact_grievance            (1 row / grievance)  --grievance_target_encounter_id--> a survey
   +-- fact_payment_rate         (1 row / rate change)
   +-- observations_long         (1 row / answered value)
```

Join keys: everything hangs off `subject_id`; facts also carry `encounter_id`,
which is their primary key and a foreign key to `encounters`. `fact_grievance`
additionally carries `grievance_target_encounter_id`, a foreign key to the
survey the grievance is about (`grievance_target_found` says whether that survey
is present in this export).

---

## Shared fact keys

`fact_self_report_survey`, `fact_grievance` and `fact_payment_rate` all begin
with the same nine columns, denormalised so each fact table is usable on its own
without a join.

| Column | dtype | Source | Meaning |
|---|---|---|---|
| `encounter_id` | string(uuid) | `Encounter.ID` | Primary key; FK to `encounters.encounter_id`. |
| `subject_id` | string(uuid) | derived (client-side join) | FK to `subjects.subject_id`. |
| `subject_name` | string | derived | Denormalised pump label. |
| `village` | factor | `location.Village` | Denormalised for grouping. |
| `district` | factor | `location.District` | Denormalised for grouping. |
| `state` | factor | `location.State` | Denormalised for grouping. |
| `submitted_by` | factor | `audit.Last modified by` (fallback `audit.Created by`) | Operator username who submitted. See [`encounters`](#encounters) for why this is not `Created by`. |
| `completed_at` | datetime (UTC) | `Encounter.Encounter date time` | Submission timestamp. |
| `completed_date` | date (local) | derived | Local calendar date of submission. |

---

## `subjects`

**Grain: one row per subject (pump).** Voided subjects are in
`_voided/subjects_voided.csv` instead.

| Column | dtype | Unit | Source | Meaning |
|---|---|---|---|---|
| `subject_id` | string(uuid) | | `Subject.ID` | Primary key of the subject. |
| `subject_external_id` | string | | `Subject.External ID` | Client-side id; often blank. |
| `subject_name` | string | | `observations.First name` | Pump label as registered. |
| `subject_type` | factor | | `Subject.Subject type` | `Pump` or `PaymentRateChart`. |
| `village` | factor | | `location.Village` | Address level 1. |
| `gram_panchayat` | factor | | `location.Gram Panchayat` | Address level 2. |
| `block` | factor | | `location.Block` | Address level 3. |
| `district` | factor | | `location.District` | Address level 4. |
| `state` | factor | | `location.State` | Address level 5. |
| `address_raw` | string | | `location{}` | Full hierarchy, pipe-joined; keeps non-standard levels the five columns above would lose. |
| `registration_date` | date | local | `Subject.Registration date` | Local calendar date the pump was registered. Avni sends this as a bare `yyyy-mm-dd`, kept as-is. |
| `subj_created_at` | datetime | UTC | `audit.Created at` | Subject creation timestamp. |
| `subj_last_modified_at` | datetime | UTC | `audit.Last modified at` | Subject last-modified timestamp. |
| `subj_created_by` | factor | | `audit.Created by` | User who registered the pump. |
| `n_encounters` | int | count | derived | Encounters attributed to this subject, all statuses. |
| `n_completed` | int | count | derived | Completed encounters for this subject. |
| `first_completed_at` | datetime | UTC | derived | Earliest completed encounter. |
| `last_completed_at` | datetime | UTC | derived | Latest completed encounter. |
| `distinct_operators` | int | count | derived | Distinct `submitted_by` users across this subject's encounters (all statuses). |
| `voided` | bool01 | | `Subject.Voided` | `1` = tombstone. Always `0` here; the `1`s live in `subjects_voided`. |

---

## `encounters`

**Grain: one row per encounter, of every type** (surveys, grievances,
payment-rate changes and any unmapped type), **and of every status** (completed,
planned, cancelled). This is the spine: the fact tables are subsets of it,
widened per form. Voided encounters are in `_voided/encounters_voided.csv`.

| Column | dtype | Unit | Source | Meaning |
|---|---|---|---|---|
| `encounter_id` | string(uuid) | | `Encounter.ID` | Primary key of the encounter. |
| `subject_id` | string(uuid) | | derived (client-side join) | FK to `subjects.subject_id`. |
| `subject_name` | string | | derived | Denormalised pump label. |
| `subject_type` | factor | | `Subject.Subject type` | `Pump` or `PaymentRateChart`. |
| `encounter_type` | factor | | `Encounter.Encounter type` | The Avni form name. |
| `encounter_category` | factor | | derived | `general` or `program`. Pumps have no programs, so these are `general`. |
| `form_version` | factor | | derived | `v1` / `v2` for the two survey forms; blank otherwise. |
| `fact_table` | factor | | derived | Which fact table this encounter feeds; blank means an unmapped encounter type. |
| `status` | factor | | derived | `completed` \| `planned` \| `cancelled`. |
| `scheduled_date` | date | local | `Encounter.Earliest scheduled date` | Local calendar date the visit window opens. Avni sends local midnight as a UTC timestamp; this is that timestamp's date in the run timezone. |
| `max_scheduled_date` | date | local | `Encounter.Max scheduled date` | Local calendar date the visit becomes overdue (the end of the visit window), encoded the same way. |
| `completed_at` | datetime | UTC | `Encounter.Encounter date time` | Submission timestamp. |
| `completed_date` | date | local | derived | Local calendar date of `completed_at`. |
| `completed_year` | int | local | derived | Local year of completion. |
| `completed_month` | int | local | derived | Local month, 1–12. |
| `completed_iso_week` | int | local | derived | Local ISO week, 1–53. |
| `completed_day_of_week` | int | local | derived | Local ISO weekday, 1 = Mon … 7 = Sun. |
| `completed_hour` | int | local | derived | Local hour of day, 0–23. |
| `cancelled_at` | datetime | UTC | `Encounter.Cancel date time` | Cancellation timestamp. A non-empty value makes `status = cancelled`. |
| `submission_lag_days` | float | days | derived | `completed_at` − the `Earliest scheduled date` timestamp, in days; negative = early. |
| `is_on_time` | bool01 | | derived | `1` if `completed_at` ≤ the `Max scheduled date` timestamp (the moment Avni treats the visit as overdue: local midnight at the start of `max_scheduled_date`) plus `--grace-days` days. Falls back to the `Earliest scheduled date` timestamp when Max is missing. Empty when the encounter is not completed or has no schedule. |
| `submitted_by` | factor | | `audit.Last modified by` (fallback `audit.Created by`) | Operator username who submitted. Not `Created by`: Avni creates the next scheduled encounter at the moment the previous one is submitted (and the first one when the pump is registered), so `Created by` names whoever did *that*. Submitting the visit edits that record, so `Last modified by` is the submitter. |
| `created_by` | factor | | `audit.Created by` | Who created the record, kept for traceability. For a scheduled visit this is the user who submitted the previous visit or registered the pump. |
| `last_modified_by` | factor | | `audit.Last modified by` | User who last edited. |
| `created_at` | datetime | UTC | `audit.Created at` | Record creation timestamp. |
| `last_modified_at` | datetime | UTC | `audit.Last modified at` | Record last-modified timestamp. |
| `program` | factor | | `ProgramEncounter.Program` | Program name; blank for general encounters. |
| `enrolment_id` | string(uuid) | | `ProgramEncounter.Enrolment ID` | FK to the enrolment; blank for general encounters. |
| `n_observations` | int | count | derived | Number of answered concepts on this encounter. |
| `attribution` | factor | | derived | How the subject link was made: `subject-id` (the encounter's own `Subject ID`) or `subject-doc-fallback` (the subject document's `encounters[]` list). See the API reference on why the server-side filter is not trusted. |
| `voided` | bool01 | | `Encounter.Voided` | `1` = tombstone; the `1`s live in `encounters_voided`. |

---

## `observations_long`

**Grain: one row per (encounter, concept, selected answer).** A scalar answer is
one row; a multi-select explodes to one row per selected value, numbered by
`list_index`. Unanswered concepts produce no row.

This is the tidy, schema-agnostic backbone the wide fact tables are derived from,
and the safety net for concepts the pipeline does not yet map: an unmapped
concept still lands here with an empty `concept_column` and is counted in
`_quality_report.md`.

| Column | dtype | Unit | Source | Meaning |
|---|---|---|---|---|
| `subject_id` | string(uuid) | | derived | FK to `subjects.subject_id`. |
| `encounter_id` | string(uuid) | | `Encounter.ID` | FK to `encounters.encounter_id`. |
| `encounter_type` | factor | | `Encounter.Encounter type` | The Avni form name. |
| `concept_name` | string | | `observations` key | The Avni concept (question) name, verbatim. |
| `concept_column` | string | | `concepts.py` | The analysis column it maps to; empty = unmapped concept. |
| `value_text` | string | | `observations` value | The value as text; one row per selected answer. |
| `value_num` | float | | derived | Numeric parse of `value_text` when it is a number; empty otherwise. |
| `value_date` | datetime | UTC | derived | Datetime parse of `value_text` when it is a date or time; empty otherwise. |
| `is_list` | bool01 | | derived | `1` if the source value was a multi-select list. |
| `list_index` | int | | derived | 0-based position within the multi-select; empty for scalars. |
| `media_url` | string(url) | URL | derived | Set when the value is a media URL (private S3; needs auth to open). |
| `submitted_by` | factor | | `audit.Last modified by` (fallback `audit.Created by`) | Operator username (same as `encounters.submitted_by`). |
| `completed_at` | datetime | UTC | `Encounter.Encounter date time` | Submission timestamp. |

---

## `fact_self_report_survey`

**Grain: one row per completed self-report survey** (the primary modelling
table). The two survey forms, v1 and v2, are unified into one schema:
`form_version` records which form a row came from, and questions that were
reworded between versions are coalesced into a single column.

Starts with the [shared fact keys](#shared-fact-keys), then:

| Column | dtype | Unit | Source | Meaning |
|---|---|---|---|---|
| `form_version` | factor | | derived | `v1` or `v2` — which survey form this row came from. |
| `chlorine_residual_mg_l` | float | mg/L | chlorine-reading concept (v1 and v2 wordings) | Free chlorine residual read off the colour strip. v2 reworded the question as a colour match, but the answer scale is the same DPD chart in mg/L, and only one wording is ever present on a given encounter. |
| `tablets_refilled_count` | int | tablets | tablets-refilled concept (v1 and v2 wordings) | Chlorine tablets added to the cartridge. |
| `tablets_refilled_source` | factor | | derived | Which wording supplied the value: `v1`, `v2` or `both`. |
| `ilc_damaged_parts_count` | int | parts | damaged-parts-count concept | Count of damaged or leaking parts on the in-line chlorinator. |
| `device_has_damage` | bool01 | | v2 damage Yes/No concept | The v2 gate question before the parts count. Empty on v1 forms — use `ilc_any_damage`. |
| `ilc_any_damage` | bool01 | | derived | `1` if `ilc_damaged_parts_count` > 0 or `device_has_damage` is true. The version-independent damage flag. |
| `pump_on_time_local` | time | HH:MM local | pump-on-time concept | Typical pump start time this week, as the operator reported it. |
| `pump_on_minutes` | int | minutes | derived | `pump_on_time_local` as minutes since local midnight. |
| `pump_off_time_local` | time | HH:MM local | pump-off-time concept | Typical pump stop time this week. |
| `pump_off_minutes` | int | minutes | derived | `pump_off_time_local` as minutes since local midnight. |
| `pump_runtime_hours` | float | hours | derived | Off − on, wrapping past midnight (+24 h if negative). |
| `last_cartridge_refill_at` | datetime | UTC | last-refill-date concept | Operator-entered date of the last cartridge refill. |
| `days_since_last_refill` | float | days | derived | `completed_at` − `last_cartridge_refill_at`. |
| `cartridge_photo_url` | string(url) | URL | cartridge-photo concept | Private S3 URL; needs auth to open (presign via `GET /media/signedUrl`). |
| `water_sample_photo_url` | string(url) | URL | water-sample-photo concept | Private S3 URL. |
| `new_tablets_photo_url` | string(url) | URL | new-tablets-photo concept | Private S3 URL; v2 only. |
| `damaged_part_photo_urls` | string(url list) | URL(s) | damaged-part-photo concept | Pipe-joined private S3 URLs. |
| `damaged_part_photo_count` | int | count | derived | Number of damaged-part photos uploaded. |
| `scheduled_date` | date | local | `Encounter.Earliest scheduled date` | Local calendar date the visit window opens. |
| `max_scheduled_date` | date | local | `Encounter.Max scheduled date` | Local calendar date the visit becomes overdue. |
| `submission_lag_days` | float | days | derived | `completed_at` − the `Earliest scheduled date` timestamp; negative = early. |
| `is_on_time` | bool01 | | derived | Same rule as `encounters.is_on_time`: `completed_at` ≤ `Max scheduled date` timestamp + grace (Earliest if Max is missing). |
| `pump_submission_seq` | int | count | derived | 1-based survey sequence within this pump. |
| `pump_prior_submissions` | int | count | derived | Surveys this pump had before this one. |
| `pump_days_since_prev_submission` | float | days | derived | Gap to this pump's previous survey. |
| `operator_submission_seq` | int | count | derived | 1-based survey sequence for this operator (`submitted_by`). |
| `operator_days_since_prev_submission` | float | days | derived | Gap to this operator's previous survey. |
| `chlorine_out_of_range_flag` | bool01 | | derived | `1` if `chlorine_residual_mg_l` falls outside [0, 5] mg/L. The row is kept, not dropped. |
| `pump_runtime_implausible_flag` | bool01 | | derived | `1` if `pump_runtime_hours` ≤ 0 or > 20. |
| `refill_in_future_flag` | bool01 | | derived | `1` if the reported refill is after the submission time. |
| `chlorine_in_target` | bool01 | | derived, optional | Only present when the run passed `--chlorine-band LOW,HIGH`. `1` if the residual falls inside that band. No health threshold is baked in by default. |

---

## `fact_grievance`

**Grain: one row per completed grievance form.** Starts with the
[shared fact keys](#shared-fact-keys), then:

| Column | dtype | Unit | Source | Meaning |
|---|---|---|---|---|
| `grievance_kind` | factor | | `Kind of Grievance` | `General` or `Survey related`. |
| `grievance_target_encounter_id` | string(uuid) | | survey-selection concept | FK to the survey encounter this grievance is about; blank for general grievances. |
| `grievance_target_found` | bool01 | | derived | `1` if that target encounter is present in this export. |
| `grievance_other_text` | string | | `Other greiveance` | Free text. The source concept name carries that typo; the column name does not. |
| `general_grievance_raw` | string | | `General Grievance` | The selected answers, pipe-joined, kept verbatim for traceability. |
| `survey_grievance_raw` | string | | `Survey Grievance` | The selected answers, pipe-joined. |
| `n_grievance_reasons` | int | count | derived | Total selected answers across both multi-selects. |

### One-hot indicator columns

When the run uses one-hot encoding (the default; `--no-onehot` turns it off and
leaves only the `*_raw` columns), each possible answer of the two multi-selects
also gets its own `bool01` column, appended after the columns above.

| Prefix | Multi-select | Meaning |
|---|---|---|
| `grv_*` | `General Grievance` | One column per general-grievance reason: cannot open the survey, insufficient chlorine tablets, no water supply in the village, chlorine device damaged, other. |
| `sgrv_*` | `Survey Grievance` | One column per survey/payment grievance reason: incorrect payment amount received, form uploaded but payment not received. |

The curated names for the answers seen so far live in `INDICATOR_NAMES` in
`concepts.py`, and **every curated column exists on every run** whether or not the
answer was selected, so the schema is stable across runs. An answer that is *not*
in that map still gets a column, named `<prefix>_<slugified answer>`, and is
reported as an unmapped answer in `_quality_report.md` — a new coded answer in
the Avni form is surfaced, never silently lost. Because the uncurated columns
depend on the data, the full indicator list for a given run is in that run's
`data_dictionary.csv`.

---

## `fact_payment_rate`

**Grain: one row per payment-rate change**, submitted against the
`PaymentRateChart` subject rather than a pump. Starts with the
[shared fact keys](#shared-fact-keys), then:

| Column | dtype | Unit | Source | Meaning |
|---|---|---|---|---|
| `rate_odisha_inr` | float | INR | `Odisha Rate` | Per-survey payment rate for Odisha. |
| `rate_andhra_pradesh_inr` | float | INR | `Andhra Pradesh Rate` | Per-survey payment rate for Andhra Pradesh. |
| `rate_effective_date` | date | local | `Effective Date` | Local calendar date the rate takes effect. Entered as a bare `yyyy-mm-dd`, kept as-is. |

---

## `operators`

**Grain: one row per submitting user**, derived entirely from
`encounters.submitted_by` (`audit."Last modified by"`, falling back to
`audit."Created by"`) on the completed encounters — not from `Created by`, which
on a scheduled visit names whoever submitted the previous one. Avni's external
API has no user endpoint, so this dimension
is built from the submissions themselves. Every count is over the encounters in
this export, so a narrowed pull (`--subject-type`, `--since`) narrows these
aggregates too.

| Column | dtype | Unit | Source | Meaning |
|---|---|---|---|---|
| `operator` | string | | `encounters.submitted_by` | Avni username. Primary key; joins to `submitted_by` everywhere else. |
| `n_submissions` | int | count | derived | Completed encounters submitted, all types. |
| `n_surveys` | int | count | derived | Self-report surveys submitted. |
| `n_grievances` | int | count | derived | Grievance forms submitted. |
| `n_payment_rates` | int | count | derived | Payment-rate forms submitted. |
| `n_other` | int | count | derived | Submissions of encounter types not mapped to a fact table. |
| `distinct_subjects` | int | count | derived | Distinct pumps this operator submitted for. |
| `first_submission_at` | datetime | UTC | derived | Earliest submission. |
| `last_submission_at` | datetime | UTC | derived | Latest submission. |
| `active_days` | int | days | derived | Distinct local dates with at least one submission. |
| `span_days` | float | days | derived | `last_submission_at` − `first_submission_at`. |
| `mean_gap_days` | float | days | derived | Mean gap between consecutive submissions. |
| `gap_cv` | float | ratio | derived | Coefficient of variation of those gaps — the consistency score. Lower = steadier. |
| `on_time_rate` | float | ratio | derived | Share of this operator's scheduled submissions with `is_on_time = 1`. |

> Operator usernames are personal identifiers. They are needed for the
> incentive analysis, but they are a reason the output tables live in Box rather
> than in this repository.

---

## `pump_schedule`

**Grain: one row per live (non-voided) `Pump` subject**, including pumps with no
survey encounters at all. Built from every non-voided survey encounter in
`encounters` (`fact_table = fact_self_report_survey`, i.e. both survey form
versions) **of any status** — planned encounters are always included here, with
or without `--include-planned`, which only affects the fact tables. Survey
encounters on a voided or non-Pump subject are not counted (the number is in
`_manifest.json` under `quality.pump_schedule`).

**How Avni schedules surveys.** Each pump has a chain of survey encounters. The
first is created when the pump is registered; each submission creates the next
planned survey within about a second, due a fixed number of days after the
submission (typically 7). Every survey has a visit window from
`Earliest scheduled date` to `Max scheduled date` (here one day apart); Avni
treats the visit as overdue from the `Max scheduled date` moment, which is
local midnight at the start of that day. A survey that is never submitted is
**not** cancelled or rescheduled, so a pump that stops submitting keeps one
stale planned survey indefinitely, and normally a pump has exactly one open
planned survey. The schedule rule itself is not exposed by the external API;
this table describes only its observable result.

**Reference time.** Every "as of" column is evaluated at the snapshot's pull
time (`_manifest.json` `source.pulled_at`), converted to the run timezone for
dates — never the wall clock — so rebuilding from the same raw snapshot gives
the same table. If `pulled_at` is missing, the as-of columns, `n_surveys_missed`,
`days_since_last_completed` and every `next_due_status` other than `none` are
empty and a warning is written.

**Overdue rule** (shared by `n_surveys_missed`, `next_due_status` and
`days_overdue`, and consistent with `encounters.is_on_time`): a planned survey is
overdue when `as_of_date >= window_end + grace_days`, where `window_end` is its
`max_scheduled_date` (its `scheduled_date` when Max is missing, the same
fallback `is_on_time` uses) and `grace_days` is the run's `--grace-days`
(recorded as `on_time_grace_days` in the manifest). That is the date-level form
of "a submission now would no longer be on time".

Rows are sorted by `state`, `district`, `village`, `subject_name`, `subject_id`.

| Column | dtype | Unit | Source | Meaning |
|---|---|---|---|---|
| `subject_id` | string(uuid) | | `Subject.ID` | Primary key; FK to `subjects.subject_id`. |
| `subject_name` | string | | derived | Denormalised pump label. |
| `village` | factor | | `location.Village` | Denormalised for grouping. |
| `gram_panchayat` | factor | | `location.Gram Panchayat` | Denormalised for grouping. |
| `block` | factor | | `location.Block` | Denormalised for grouping. |
| `district` | factor | | `location.District` | Denormalised for grouping. |
| `state` | factor | | `location.State` | Denormalised for grouping. |
| `registration_date` | date | local | `Subject.Registration date` | Local calendar date the pump was registered. |
| `as_of_at` | datetime | UTC | `meta.pulled_at` | Reference moment for the status columns: when the snapshot was pulled. Same value on every row. |
| `as_of_date` | date | local | derived | Local calendar date of `as_of_at`. |
| `n_surveys_scheduled` | int | count | derived | Survey encounters on this pump, any status. Every survey is a scheduled visit, so this is the length of the pump's chain. Equals `n_surveys_completed + n_surveys_cancelled + n_open_planned`. |
| `n_surveys_completed` | int | count | derived | Survey encounters with `status = completed`. |
| `n_surveys_cancelled` | int | count | derived | Survey encounters with `status = cancelled`. |
| `n_open_planned` | int | count | derived | Survey encounters with `status = planned` (neither submitted nor cancelled). Normally 1; 0 means the chain has ended or never started. |
| `multiple_open_planned` | bool01 | | derived | 1 if `n_open_planned > 1` — unexpected under the scheduling behaviour above; also counted in `_quality_report.md` warnings. |
| `n_surveys_missed` | int | count | derived | Planned surveys that are overdue as of `as_of_date` (overdue rule above). Surveys submitted late are *not* missed; see `n_on_time`. |
| `n_on_time` | int | count | derived | Completed surveys with `encounters.is_on_time = 1`. |
| `on_time_rate` | float | ratio | derived | `n_on_time` / completed surveys with a known `is_on_time` (i.e. with a schedule). Empty when there are none. |
| `first_survey_due_date` | date | local | derived | Earliest `scheduled_date` across this pump's surveys, any status. |
| `first_completed_at` | datetime | UTC | derived | Earliest survey `completed_at`. |
| `first_completed_date` | date | local | derived | Local calendar date of `first_completed_at`. |
| `last_completed_at` | datetime | UTC | derived | Latest survey `completed_at`. |
| `last_completed_date` | date | local | derived | Local calendar date of `last_completed_at`. |
| `last_submitted_by` | factor | | derived | `submitted_by` of the latest completed survey. |
| `days_since_last_completed` | float | days | derived | `as_of_at` − `last_completed_at`. Empty if the pump never submitted. |
| `next_due_encounter_id` | string(uuid) | | derived | FK to `encounters.encounter_id`: the open planned survey with the latest `scheduled_date` (ties broken by window end, then id). Empty when `n_open_planned = 0`. |
| `next_due_date` | date | local | `Encounter.Earliest scheduled date` | `scheduled_date` of that survey: the day its window opens. |
| `next_due_window_end_date` | date | local | `Encounter.Max scheduled date` | `max_scheduled_date` of that survey (its `scheduled_date` when Max is missing): the local day it becomes overdue. |
| `next_due_status` | factor | | derived | `upcoming` if `as_of_date < next_due_date`; `overdue` per the overdue rule; `due` otherwise (inside the window); `none` if there is no open planned survey. Empty only when the reference time or the survey's dates are missing. |
| `days_overdue` | int | days | derived | `as_of_date − next_due_window_end_date` in whole days when `next_due_status = overdue` (0 on the first overdue day when `grace_days = 0`); empty otherwise. |

> A pump that has stopped submitting shows `next_due_status = overdue` with a
> growing `days_overdue`, `n_surveys_missed = 1`, and a `next_due_date` that
> stays fixed: Avni does not move the stale visit forward. `n_surveys_missed`
> therefore counts missed *planned visits on record*, not the number of weekly
> visits that would have fallen due since; derive that from
> `days_since_last_completed` if needed.

---

## Sidecar files

Written alongside the eight tables in the same output directory.

| File | Grain | What it is |
|---|---|---|
| `data_dictionary.csv` | 1 row per column of every table | `table, column, dtype, unit, source_concept, description, example`. **The authoritative dictionary for that run**, including every indicator column actually emitted. Its `example` column holds real values, so it stays in Box. |
| `_manifest.json` | — | Run metadata: when the pull happened, server, API version, options used (timezone, `include_planned`, one-hot, grace days) and per-table row counts. The record of which pull and which options produced the outputs. |
| `_quality_report.md` | — | Data-quality summary: unmapped concepts, unmapped coded answers, orphan grievance targets, duplicate ids, and the `*_flag` counts. Read this before modelling. |
| `_voided/subjects_voided.csv` | 1 row per voided subject | Tombstoned subjects, same columns as `subjects`, with `voided = 1`. Only written when there are any. |
| `_voided/encounters_voided.csv` | 1 row per voided encounter | Tombstoned encounters, same columns as `encounters`. Only written when there are any. |
| `encounters_wide.csv` | 1 row per encounter | Optional convenience export (`--big-table`): every concept as a column, sparse across form types. Derived from the tables above, never the source of truth. |
| `report.html` | — | Self-contained interactive report over the same tables; no CDN, no external assets. |
| `media/` | — | Field photos downloaded by `--fetch-media` / `fetch_media.py`, plus `_media_manifest.csv` mapping each S3 URL to its local copy, byte size and sha256. |
| `_raw.json` | — | The raw API snapshot a run was built from, when `--save-raw` was used. Archived append-only into Box `12-avni/120-raw/`. |
