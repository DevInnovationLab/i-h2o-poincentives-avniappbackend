# Pipeline build spec (historical)

> **This is a historical build spec, not current documentation.** It is the
> design document the pipeline was built from, kept for the reasoning behind the
> table set, the encoding rules and the open questions — not as a description of
> what the code does today. Where it disagrees with the code, the code wins.
>
> **What was specified here and never built:**
>
> - **§9 R datastore** — the `.rds` / `.RData` outputs, `emit_rds.py` and
>   `build_rds.R`. No R file exists anywhere in this project; the pipeline emits
>   CSV (plus the HTML report) only. The typing information those outputs would
>   have carried lives in `data_dictionary.csv` instead.
> - **§9 Parquet** (`--format parquet`) — not implemented.
> - **§12 `validate.py`** — never written as a separate module. The checks it
>   describes were folded into the transform step and are reported in
>   `_quality_report.md`.
>
> **What changed in the build:** the package is `avni_pipeline/`, not
> `avni_export/`; the `client.py`, `model.py` and `join.py` split in §3 collapsed
> into `pull.py` and `transform.py`; and the code now lives under
> `2-code/20-programs/` and `2-code/21-wrangling/` rather than at the repo root.
>
> For what the pipeline actually produces, see
> [`../41-data-dictionaries/avni-tables.md`](../41-data-dictionaries/avni-tables.md).
> For the API it reads, see
> [`../40-data-sources/avni-api.md`](../40-data-sources/avni-api.md).

**Goal.** Build a pipeline that pulls the DIL water-pump data out of Avni and
writes it as a set of **regression-ready, semantically-named tables** (CSV) plus
an **R datastore** (`.rds` / `.RData`). Downstream data scientists will run
regressions (chlorine level, operator consistency, grievances, payments), so the
output must be typed, tidy, joinable, and documented.

> **Source of truth is the API, not the text dumps.** Pull straight from Avni.
> At the time this was written there were text dumps of pulled subjects on disk,
> parsed by a predecessor script that is no longer in this repository. They were
> lossy (no full audit timestamps, no external-IDs, no location IDs) and were
> never the primary path. They are gone: they held real field data, so they are
> not part of a public repository. Replay a saved raw API snapshot
> (`--source raw --input _raw.json`) if you need an offline run.

This document is the build spec. It is intentionally detailed; an implementing
agent should be able to build from it without re-deriving decisions. Where a
decision needs the product owner, it is listed in **§13 Open questions**.

---

## 1. Scope & entities

| Entity | Avni source | Grain | Notes |
|---|---|---|---|
| Subject (Pump / PaymentRateChart) | `GET /api/subjects` + `GET /api/subject/{id}` | 1 row / subject | address hierarchy, `observations`, `encounters[]`, audit |
| General Encounter | `GET /api/encounters` | 1 row / encounter | the "Completed visits": Self-report Survey (v1/v2), Greiveance form, Payment Rate |
| ProgramEnrolment / ProgramEncounter | `GET /api/programEnrolment(s)` | — | **none for Pump today** (0 enrolments); build generically but expect empty |
| Observation | inside each encounter/subject `observations{}` | 1 row / (encounter, concept) | EAV; the atomic fact |
| Operator (user) | `audit."Created by"` on encounters | 1 row / user | derived dimension, no dedicated endpoint |

Use `version=2` (user-entered dates as `yyyy-MM-dd`; audit stays full ISO).

---

## 2. Critical correctness carry-overs (do NOT regress these)

1. **Subject↔encounter join is client-side.** The server ignores
   `/api/encounters?subjectId=` and returns org-wide encounters. Attribute each
   encounter by its **own `"Subject ID"`** (`encounter["Subject ID"] == subject["ID"]`),
   with the subject document's `encounters[]` uuid list as a fallback. This is
   was already implemented in a predecessor script, no longer in this
   repository — **port that logic; never trust the server filter.** (It now lives
   in `pull.py`, and `encounters.attribution` records which path was used.)
2. **Token lifetime.** A full-org pull can exceed the ~1h id-token lifetime. Use
   `avni_auth.AvniClient` (auto Cognito refresh), *not* raw `urllib` with a static
   token. `client.get_all_pages()` already handles cursor pagination.
3. **Voided records are returned.** Treat `Voided == true` as tombstones: exclude
   from analysis tables by default, but keep a `voided` boolean column and/or a
   `*_voided.csv` so nothing is silently dropped.
4. **`totalElements` is per-page** — page until `content` is empty (already handled
   in `get_all_pages`).
5. **Audit = who/when.** `audit."Created by"` → `submitted_by` (the operator),
   `audit."Created at"` → `created_at`, `audit."Last modified by"/"at"` →
   `last_modified_by`/`last_modified_at`.

---

## 3. Pipeline architecture

```
avni_export/
  __init__.py
  client.py         # thin wrapper over avni_auth.AvniClient (auth, paging, retries)
  pull.py           # raw fetch: subjects, subject docs, ALL encounters (once), enrolments
  model.py          # dataclasses / typed dicts for Subject, Encounter, Observation
  join.py           # Subject-ID index + attach encounters to subjects (the fix)
  concepts.py       # concept-name -> (column, dtype, unit) mapping + factor levels
  transform.py      # normalize -> long table -> wide facts -> derived features -> encode
  emit_csv.py       # write the CSV tables + data_dictionary.csv + _manifest.json
  emit_rds.py       # write .rds per table + combined .RData (pyreadr), factors set
  validate.py       # data-quality checks (§12)
avni_to_tables.py   # CLI entrypoint that wires the above
build_rds.R         # OPTIONAL R-native alternative (reads CSVs, saves .rds) — see §9
```

**CLI**
```bash
python3 avni_to_tables.py \
  --out data/ \
  --subject-type Pump \          # optional filter; omit for all types
  --format csv,rds,parquet \     # parquet optional bonus (§9)
  --since 1900-01-01T00:00:00.001Z \   # incremental cursor (optional)
  --include-planned \            # keep scheduled-only encounters as rows (default: off)
  --onehot-multiselect \         # default on; else pipe-delimited (§8)
  --tz Asia/Kolkata              # local tz for time-of-day math (§7)
```
Auth via env (`AVNI_AUTH_TOKEN` or `AVNI_USERNAME`/`AVNI_PASSWORD` for Cognito),
exactly like the existing scripts. Fail fast with the README's 401/500 hints.

**Pull order** (minimizes calls, applies the fix):
1. `subjects = client.get_all_pages("/api/subjects", subjectType=...)`
2. For each, `client.get(f"/api/subject/{id}")` → full doc (address, obs, `encounters[]`, audit).
3. `all_encounters = client.get_all_pages("/api/encounters")` **once**; index by `"Subject ID"`.
4. Attach encounters to subjects via the index (fallback to `encounters[]`).
5. (Generic) walk `enrolments[]` → `programEnrolment/{id}` → `programEncounters` — expect empty for Pump.

---

## 4. Output table set — **recommended: star schema ("a list of CSVs")**

One dimension per real-world thing, one fact per encounter type (consistent
schema per type = clean regression). All facts carry FKs so scientists can join.

| File | Grain | Purpose |
|---|---|---|
| `subjects.csv` | 1 / pump | dimension: name, type, external_id, full address, registration, voided, audit |
| `operators.csv` | 1 / user | dimension: username + derived activity aggregates (§7) |
| `encounters.csv` | 1 / encounter (all types) | spine: common fields + status + dates + audit + FK subject |
| `fact_self_report_survey.csv` | 1 / survey | ★ **primary regression table** — typed obs + derived features (v1 & v2 unified) |
| `fact_grievance.csv` | 1 / grievance | one-hot grievance categories + FK to target survey |
| `fact_payment_rate.csv` | 1 / rate change | state rates + effective date |
| `observations_long.csv` | 1 / (encounter, concept) | tidy EAV source-of-truth (schema-agnostic; pivot-ready) |
| `data_dictionary.csv` | 1 / column | column, table, dtype, unit, source_concept, description, example |
| `_manifest.json` | — | run metadata: pulled_at, server, org, version, row counts, token subject, git sha |

Join keys: everything hangs off `subject_id`; facts also carry `encounter_id`;
grievances carry `grievance_target_encounter_id` (FK to a survey).

### CSV-structure options (pick per §13; default = Option A)

- **Option A — Star schema (RECOMMENDED).** Typed facts + dimensions above.
  *Pros:* per-type schemas are clean and dense; `fact_self_report_survey.csv` is
  directly regressible; joins are explicit. *Cons:* several files.
- **Option B — One Big Table.** Single `encounters_wide.csv`, one row/encounter,
  every concept a column, NULLs across types. *Pros:* one file, trivial load.
  *Cons:* sparse, mixed grain, NULL-heavy, awkward to regress across types.
  Offer as a *convenience export* generated from Option A, not the primary.
- **Option C — Tidy long only.** Just `observations_long.csv` (+ dims). *Pros:*
  fully general, new concepts need no schema change, great for pivoting. *Cons:*
  scientists must pivot+type before modeling; dtypes live in a `value_num` column.
  **Ship C alongside A** — long table is the audit-friendly backbone; A is the
  ergonomic surface.

> Ship **A + C** by default (star schema for modeling, long table for flexibility),
> and generate **B** on request via `--big-table`.

---

## 5. Semantic column dictionary (concept → column)

Snake_case, units in the name where useful. `dtype` drives CSV/RDS typing.
Multi-selects become indicator columns (§8). Verify names against live metadata
(observation keys ARE concept names; a concept could be renamed in Admin).

### 5.1 Dimensions (subjects.csv / encounters.csv)

| Column | dtype | Source | Notes |
|---|---|---|---|
| `subject_id` | str (uuid) | subject `ID` | PK |
| `subject_external_id` | str | `External ID` | may be null |
| `subject_name` | str | `observations."First name"` / Name | pump label |
| `subject_type` | factor | `Subject type` | Pump / PaymentRateChart |
| `village`,`gram_panchayat`,`block`,`district`,`state` | factor | `location{}` | drop the `* External ID` and `None`s |
| `registration_date` | date | `Registration date` | |
| `voided` | bool | `Voided` | |
| `subj_created_at`,`subj_last_modified_at` | datetime(UTC) | subject `audit` | |
| `encounter_id` | str (uuid) | encounter `ID` | PK of encounters.csv |
| `encounter_type` | factor | `Encounter type` | Self-report Survey / Self-report survey (v2) / Greiveance form / Payment Rate |
| `encounter_category` | factor | — | general / program |
| `status` | factor | derived | completed / planned / cancelled |
| `scheduled_date` | date | `Earliest scheduled date` | |
| `completed_at` | datetime(UTC) | `Encounter date time` | |
| `cancelled_at` | datetime(UTC) | `Cancel datetime` | |
| `submitted_by` | factor | `audit."Created by"` | operator username |
| `last_modified_by` | factor | `audit."Last modified by"` | |
| `created_at`,`last_modified_at` | datetime(UTC) | encounter `audit` | |
| `program` | factor | enrolment `Program` | null for Pump |

### 5.2 Self-report Survey facts (v1 + v2 unified)

| Column | dtype | Source concept | Unit/notes |
|---|---|---|---|
| `chlorine_residual_mg_l` | float | "What is the chlorine reading?" | mg/L; flag implausible values (§12) |
| `tablets_refilled_count` | int | "How many tablets did you refill?" **or** v2 "Number of tablets refilled" | coalesce v1/v2 into one column; keep `tablets_refilled_source` = v1/v2 |
| `ilc_damaged_parts_count` | int | "How many parts of the ILC device are damaged or leaking?" | count |
| `device_has_damage` | bool | v2 "Are there any parts of the chlorine device which are damaged or leaking?" | Yes/No → 1/0; null for v1 (derive from count>0) |
| `pump_on_time_local` | time | "This week, what time did you normally turn on the water pump?" | "HH:MM" local |
| `pump_off_time_local` | time | "This week, what time did you normally turn off the water pump?" | "HH:MM" local |
| `last_cartridge_refill_at` | datetime | "When did you last refill the cartridge?…" | user-entered |
| `cartridge_photo_url` | str | "Please open the lid…take a clear picture…cartridge…" | media (private; see §11) |
| `water_sample_photo_url` | str | "…picture of the water sample…colour strip" | media |
| `damaged_part_photo_urls` | str (pipe-joined) | "…picture of the damaged or leaking device part" | list → `count` also emitted |
| `new_tablets_photo_url` | str | v2 "…picture of the new chlorine tablets…" | media |
| `form_version` | factor | encounter_type | v1 / v2 |

### 5.3 Grievance facts (one-hot)

| Column | dtype | Source | Notes |
|---|---|---|---|
| `grievance_kind` | factor | "Kind of Grievance" | General / Survey related |
| `grievance_target_encounter_id` | str (uuid) | "Select the survey for which this grievance is raised" | FK → survey |
| `grievance_other_text` | str | "Other greiveance" | free text |
| `grv_cannot_open_survey` | bool | "General Grievance" ∋ "I can not open the survey" | one-hot |
| `grv_insufficient_tablets` | bool | ∋ "I do not have enough chlorine tablets…" | one-hot |
| `grv_no_water_supply` | bool | ∋ "There was no water suply in the village…" | one-hot (note source typo) |
| `grv_device_damaged` | bool | ∋ "The chlorine device is damaged…" | one-hot |
| `grv_other` | bool | ∋ "Other" | one-hot |
| `sgrv_incorrect_payment` | bool | "Survey Grievance" ∋ "I have received an incorrect payment amount" | one-hot |
| `sgrv_uploaded_no_payment` | bool | ∋ "I uploaded the form correctly but did not recieve the payment" | one-hot (note typo) |

> Multi-select answer strings are the coded values verbatim from the data — build
> the indicator map from **live distinct values**, don't hardcode from this list;
> log any unseen answer so the map stays complete.

### 5.4 Payment Rate facts

| Column | dtype | Source | Notes |
|---|---|---|---|
| `rate_odisha_inr` | float | "Odisha Rate" | ₹ |
| `rate_andhra_pradesh_inr` | float | "Andhra Pradesh Rate" | ₹ |
| `rate_effective_date` | date | "Effective Date" | |

---

## 6. `observations_long.csv` (tidy backbone)

`subject_id, encounter_id, encounter_type, concept_name, concept_column, value_text,
value_num, value_date, is_list, list_index, media_url, submitted_by, completed_at`

- One row per scalar; **multi-selects explode to one row per selected value**
  (`list_index`). `value_num` populated when numeric; `value_date` when a date;
  `media_url` when the value is an image/file URL. This is the schema-agnostic
  source the wide facts are derived from and the safety net for concepts we didn't
  map.

---

## 7. Derived features (regression fuel)

Computed in `transform.py`; documented in `data_dictionary.csv`.

**Time / calendar (from `completed_at`, tz-aware — §13):**
`completed_date, completed_year, completed_month, completed_iso_week,
completed_day_of_week, completed_hour`.

**Cadence / consistency (the operator-incentive analysis):**
- `submission_lag_days` = `completed_at` − `scheduled_date` (signed).
- `is_on_time` = `completed_at` ≤ `scheduled_date` (+grace, §13).
- Per **pump**: `pump_submission_seq`, `pump_prior_submissions`,
  `pump_days_since_prev_submission`.
- Per **operator**: `operator_submission_seq`, `operator_total_submissions`,
  `operator_active_days`, `operator_mean_gap_days`, `operator_gap_cv`
  (coefficient of variation of inter-submission gaps = "consistency" score),
  `operator_distinct_pumps`.
- `operators.csv` = these aggregated to 1 row/operator.

**Domain (pump/chlorine):**
- `pump_runtime_hours` = `pump_off_time_local` − `pump_on_time_local`
  (handle overnight wrap → add 24h if negative; flag implausible in §12).
- `days_since_last_refill` = `completed_at` − `last_cartridge_refill_at`.
- `chlorine_in_target` = band flag — **configurable**, default off; if enabled use a
  documented band (e.g. WHO free residual 0.2–2.0 mg/L at point of use). Do **not**
  bake in a health threshold without owner sign-off (§13); ship the raw value regardless.
- `ilc_any_damage` = `ilc_damaged_parts_count` > 0.

---

## 8. Encoding rules (regression-friendly)

- **Booleans → `0/1` integers** in CSV (models like numerics); provide TRUE/FALSE
  factors in the R store. Document the convention once.
- **Multi-select → one-hot** indicators (default, `--onehot-multiselect`). Keep an
  optional pipe-delimited raw column too (`*_raw`) for traceability. Alternative:
  pure pipe-delimited (`--no-onehot`) for scientists who prefer to encode themselves.
- **Times "HH:MM" → also emit numeric** `*_minutes` (minutes since midnight) and
  keep the string. Regressions use the numeric; humans read the string.
- **Dates → ISO 8601 string** + numeric parts (§7). Datetimes are **UTC** (`…Z`);
  user time-of-day fields are **local** — never mix in arithmetic without tz (§13).
- **Categoricals** (subject_type, encounter_type, status, grievance_kind,
  submitted_by, admin levels) → string in CSV; **factors** in R with explicit,
  stable level order (define in `concepts.py`).
- **Missing** → empty cell in CSV (documented as NA); real `NA` in R; never a
  sentinel like `-1`/`0` that a model would read as data.
- **IDs stay strings** (uuids), never coerced to numbers.
- **Units live in the column name** (`_mg_l`, `_inr`, `_count`, `_minutes`, `_days`, `_hours`).

---

## 9. R datastore

**Primary (single Python pipeline): `pyreadr`.**
- Write **per-table `.rds`** (`rds/subjects.rds`, `rds/fact_self_report_survey.rds`, …)
  and a combined **`avni_data.RData`** holding every frame (names = table names).
- Set dtypes before writing: factors for categoricals (with defined levels),
  `Date`/`POSIXct` for dates/datetimes (POSIXct in **UTC**), integer vs double
  correctly, logical for booleans. `pyreadr.write_rds` / `write_rdata`.
- Downstream: `load("avni_data.RData")` or `readRDS("rds/fact_self_report_survey.rds")`.

**Alternative (R-native, reproducible): `build_rds.R`.**
- Reads the CSVs with `readr::read_csv` using an explicit `col_types` spec derived
  from `data_dictionary.csv`, applies `factor()` with the documented levels,
  parses dates/datetimes, then `saveRDS()` per table + `save.image()`.
- Ship both; `build_rds.R` is the escape hatch if the team wants R to own typing.

**Bonus: Parquet** (`--format parquet`). Preserves dtypes cross-tool (R `arrow`,
Python, DuckDB). Cheap to add; recommend shipping it too.

---

## 10. Deliverable layout

```
data/
  subjects.csv  operators.csv  encounters.csv
  fact_self_report_survey.csv  fact_grievance.csv  fact_payment_rate.csv
  observations_long.csv
  encounters_wide.csv            # only with --big-table (Option B)
  data_dictionary.csv  _manifest.json
  rds/*.rds  avni_data.RData
  parquet/*.parquet              # only with --format parquet
  _voided/*.csv                  # voided rows pulled out (audit trail)
build_rds.R
```

---

## 11. Media / photos (chlorine, cartridge, water-sample, damaged-part)

- Values are **private S3 URLs** (`prod-user-media`) → bare GET = `403`. They are
  **not needed for regression**, so by default emit them as **string URL columns**
  only (plus `*_count` for the list fields). Flag in the dictionary that they
  require auth to open.
- Optional `--resolve-media`: batch-presign via `POST /media/signedUrls`
  (header `auth-token`) → `{url: presignedUrl}`, and write a side table
  `media_index.csv (encounter_id, concept_column, s3_url, presigned_url,
  presigned_expires_at)`. Presigned URLs expire — treat as ephemeral, don't bake
  into the modeling tables. (Image *embedding* for the HTML report is a separate
  track — see the report work, out of scope here.)

---

## 12. Validation & data-quality report (`validate.py` → `_quality_report.md`)

Fail loud on structural problems; warn on domain oddities:
- **Row-count reconciliation**: encounters attributed == sum over subjects;
  assert no encounter lands on >1 subject.
- **Orphans**: `grievance_target_encounter_id` not found in surveys.
- **Duplicates**: repeated `encounter_id`; near-duplicate surveys (same pump+day).
- **Domain flags** (rows kept, flagged with boolean `*_flag` columns): chlorine
  outside [0, 5]; `pump_runtime_hours` ≤ 0 or > 20;
  `days_since_last_refill` < 0; `submission_lag_days` wildly negative;
  `last_cartridge_refill_at` in the future.
- **Operator sanity**: distinct `submitted_by` count (if ~1, operators share a
  login → the incentive analysis is blocked; surface prominently).
- **Coverage**: % encounters with no observations (planned rows), unmapped concepts
  (anything in `observations_long` with no `concept_column`).

---

## 13. Open questions (product owner decisions before/while building)

1. **In-scope encounter types for modeling** — surveys only, or grievances/payments too?
2. **Planned (scheduled-only, no-observation) encounters** — rows with NA, or excluded? (default: excluded, `--include-planned` to keep).
3. **Chlorine target band & units** — confirm mg/L and the acceptable range; or ship raw only (default).
4. **Timezone for time-of-day math** — confirm local tz is `Asia/Kolkata` (IST); needed for `pump_runtime_hours` since datetimes are UTC but on/off times are local.
5. **On-time grace window** — how many days late still counts as `is_on_time`?
6. **Multi-select encoding** — one-hot (default) vs pipe-delimited vs both.
7. **v1/v2 tablet fields** — confirm "How many tablets did you refill?" (v1) and "Number of tablets refilled" (v2) are the same measure to coalesce.
8. **PII** — keep `submitted_by` usernames and media URLs as-is, or pseudonymize operators (stable hash) for sharing?
9. **NA convention** — empty cell (default) vs literal `NA` string.
10. **R typing** — booleans as 0/1 or TRUE/FALSE; categoricals as factors (which level order)?
11. **Incremental vs full** — one-shot full export, or persist a `lastModifiedDateTime` cursor for deltas?

---

## 14. Acceptance criteria

- `python3 avni_to_tables.py --out data/ --subject-type Pump` runs end-to-end on a
  fresh token and writes every file in §10 with no traceback.
- Encounter attribution matches the Subject-ID join (spot-check a handful of
  pumps against their visit counts in the Avni web app).
- `fact_self_report_survey.csv` loads in pandas **and** R with correct dtypes;
  `chlorine_residual_mg_l` is numeric; `submitted_by`/admin levels are factors in R.
- `data_dictionary.csv` documents **every** column in every table (name, table,
  dtype, unit, source_concept, description).
- `avni_data.RData` loads via `load()`; each frame row count matches its CSV.
- `_manifest.json` records pulled_at, server, org, version, per-table row counts.
- `_quality_report.md` produced; no unmapped concepts silently dropped.
- Re-running is deterministic (stable column order, stable factor levels, sorted rows).

---

## 15. Reuse map (what already exists)

| Need | Reuse |
|---|---|
| Auth + token refresh + paging | `avni_auth.AvniClient` (`.get`, `.get_all_pages`) |
| Subject-ID join (the fix) | a predecessor script, no longer in this repository (logic now in `pull.py`) |
| Value typing (number/list/image detection) | a predecessor script, no longer in this repository (logic now in `concepts.py`) |
| Address parsing | a predecessor script, no longer in this repository (logic now in `transform.py`) |
| Concept→column starter map | §5 of this doc |
| Offline replay (no API) | a saved raw API snapshot, `--source raw` |

---

### Build order (suggested)
1. `client.py` + `pull.py` (raw fetch, apply Subject-ID join) → dump raw JSON to verify counts.
2. `concepts.py` + `observations_long.csv` (tidy backbone) → validates the mapping end-to-end.
3. `subjects.csv` / `encounters.csv` dimensions.
4. `fact_self_report_survey.csv` (+ derived §7) — the money table; get scientists looking early.
5. `fact_grievance.csv`, `fact_payment_rate.csv`, `operators.csv`.
6. `data_dictionary.csv`, `_manifest.json`, `validate.py`.
7. `emit_rds.py` + `build_rds.R`; optional Parquet + Option B big table.
