# i-h2o POIncentives — Avni app backend

The data backend for the **i-h2o POIncentives** study. It pulls pump-operator
records out of the [Avni](https://avniproject.org) field-data-collection
platform, transforms them into tidy, analysis-ready CSV tables, renders a
self-contained interactive HTML report, downloads the field photos, and
publishes the result to Box.

Everything here is code and documentation. **No study data lives in this
repository** — see [Where the data lives](#where-the-data-lives).

## Repository map

```
Main.py                  entry point: wraps the scripts in 2-code/
1-data/12-avni/          local landing area for a pull (git-ignored, empty here)
2-code/20-programs/      avni_auth.py (the API client) + avni_pipeline/ (the library)
2-code/21-wrangling/     avni_to_tables.py, fetch_media.py, sync_box.py, review_app.py
3-output/31-notebooks/   analysis notebooks
4-documentation/         data-source reference, data dictionaries, design docs
```

## Quick start

```bash
# 1. credentials: copy .env.example to .env and fill it in, or export directly
export AVNI_SERVER="https://app.avniproject.org"
export AVNI_AUTH_TOKEN="<cognito id token>"

# 2. pull -> CSV tables + HTML report, keeping the raw snapshot it was built from
python3 Main.py tables --subject-type Pump \
    --out 1-data/12-avni/121-clean/ \
    --save-raw 1-data/12-avni/120-raw/_raw.json

# 3. download the field photos and repoint the report at local copies
python3 2-code/21-wrangling/fetch_media.py --dir 1-data/12-avni/121-clean/

# 4. publish to Box (always --dry-run first)
python3 2-code/21-wrangling/sync_box.py --dry-run \
    --dir 1-data/12-avni/121-clean/ --raw 1-data/12-avni/120-raw/_raw.json
```

## Reviewing submissions

```bash
python3 Main.py review --village "Village A" --village "Village B"
```

Opens a local page (127.0.0.1 only) for one review period at a time (periods
end on the 13th and 27th of each month, India time). Each pump follows Avni's
rolling 5-day schedule (the next survey is due 5 days after the last one came
in); every cycle shows its survey, labelled on time or overdue, or a missed slot, and both get Approve / Reject buttons that
take a reason. Grievance and payment-issue forms are reviewed alongside. The app
is connected to **Box**, the system of record: it reads
`12-avni/121-clean/` from Box Drive and appends every decision to
`12-avni/123-reviews/review_log.csv` there, summarised in `review_status.csv`
(the review sheet: one row per pump per 5-day cycle, ready for Excel or Google Sheets). A fresh pull shows
up once it is published with `Main.py box`. Decisions survive restarts and
re-pulls. See
[`review-tables.md`](4-documentation/41-data-dictionaries/review-tables.md).

The pipeline is plain Python 3 (standard library only) except for **boto3**,
which is needed only for programmatic Cognito login:

```bash
pip install boto3
```

`avni_to_tables.py` takes three sources: `api` (live pull, needs credentials),
`raw` (replay a saved `_raw.json` snapshot) and `csv` (re-render the report from
tables already on disk). Only `api` needs credentials, so most reruns do not.

**If you get an HTTP 500 from the API, it is almost certainly the token, not your
request** — Avni wants the Cognito *id* token, not the access token. That, and
the rest of the auth flow, is in the
[Avni API reference](4-documentation/40-data-sources/avni-api.md).

## Where the data lives

In **Box**, not here. Access requires membership of the study's Box folder —
ask the study lead.

| Box folder | What is in it | Rule |
|---|---|---|
| `12-avni/120-raw/` | Raw API snapshots, `pull_<UTC>.json` | **Append-only.** A pull can never be reproduced, so a snapshot is evidence and is never overwritten. |
| `12-avni/121-clean/` | The CSV tables, `data_dictionary.csv`, `_manifest.json`, `_quality_report.md`, `report.html`, `media/` | **Overwritten every run.** Everything in it is derived from a snapshot by code. |
| `12-avni/123-reviews/` | `review_log.csv`, `review_status.csv` | **Append-only log.** Reviewer decisions from the review UI; human input, never regenerated. |

`1-data/12-avni/` mirrors that layout locally and is git-ignored. Nothing under
it, and no `.env`, should ever be committed: the tables carry operator usernames
and pump locations, and the field photos are study data.

`2-code/21-wrangling/sync_box.py` is the only place Box paths appear; the
pipeline itself writes a local directory and knows nothing about where it is
published.

## Documentation

| Document | What it covers |
|---|---|
| [`4-documentation/40-data-sources/avni-api.md`](4-documentation/40-data-sources/avni-api.md) | **Start here.** The Avni API: access, the Cognito auth flow and the 500 trap, environment variables, the data model and join keys, every endpoint, pagination, the entity field reference, media handling, debugging history and gotchas. |
| [`4-documentation/41-data-dictionaries/avni-tables.md`](4-documentation/41-data-dictionaries/avni-tables.md) | The eight output tables: grain, columns, dtypes, and the encoding contract. |
| [`4-documentation/41-data-dictionaries/review-tables.md`](4-documentation/41-data-dictionaries/review-tables.md) | The two review CSVs written by the review UI. |
| [`4-documentation/42-design/pipeline-build-spec.md`](4-documentation/42-design/pipeline-build-spec.md) | The original build spec. **Historical** — parts of it were never built. |
| [`AGENTS.md`](AGENTS.md) | Conventions for coding agents working in this repo. |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | How to set up, run and contribute. |

The authoritative, run-specific data dictionary is `data_dictionary.csv` in Box
`12-avni/121-clean/`; it holds example values from live rows, which is why it is
not published here.

## Conventions

This repo follows the DIL folder convention: numbered top-level folders
(`1-data`, `2-code`, `3-output`, `4-documentation`) with numbered subfolders, so
a path tells you what a file is for. Raw inputs are never edited in place;
everything downstream is regenerated from them by code.

## Contact

Maintained by the [Development Innovation Lab](https://bfi.uchicago.edu/land/development-innovation-lab/).
Data access, Avni accounts and Box membership are handled by the study team, not
through this repository.
