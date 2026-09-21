# Data source — the Avni API

Origin, provider and access instructions for the raw source behind this repo.

Everything in `1-data/12-avni/` originates from the **Avni** field-data-collection
platform (`https://app.avniproject.org`), read through Avni's **external REST
API**. This document is the working reference for that API: the auth flow, the
data model, the join keys, every endpoint we call, the debugging history that got
us here, and the gotchas we actually hit.

- **Provider**: Avni (`avniproject.org`), a field-data-collection platform.
- **Instance**: the **production** server `https://app.avniproject.org`.
- **Org**: `po_uat` (the org shortname; it is part of every username).
- **Access**: an Avni account on that org with the `user` role. Request one from
  the study's Avni administrator. There is no anonymous or read-only key.
- **Official docs**: https://avni.readme.io/docs/api-guide
- **OpenAPI spec**: https://raw.githubusercontent.com/avniproject/avni-server/master/avni-server-api/src/main/resources/api/external-api.yaml

---

## Table of contents

1. [What is in this source](#what-is-in-this-source)
2. [Authentication (read this first — it explains the 500s)](#authentication-read-this-first--it-explains-the-500s)
3. [Environment variables](#environment-variables)
4. [Two layers: metadata vs data](#two-layers-metadata-vs-data)
5. [Data model (entities & how they link)](#data-model-entities--how-they-link)
6. [Endpoints we use](#endpoints-we-use)
7. [Pagination & versioning](#pagination--versioning)
8. [Entity field reference](#entity-field-reference)
9. [Media / field photos](#media--field-photos)
10. [Code that talks to this source](#code-that-talks-to-this-source)
11. [Debugging history / root-cause writeup](#debugging-history--root-cause-writeup)
12. [Gotchas checklist](#gotchas-checklist)
13. [Next steps](#next-steps)

---

## What is in this source

Avni hosts the i-h2o POIncentives pump-operator program. Pump operators submit
forms from the Avni mobile app; this repo reads those submissions back out.

| Thing | Value |
|---|---|
| Base URL (`AVNI_SERVER`) | `https://app.avniproject.org` |
| Org shortname | `po_uat` |
| Cognito user pool | `ap-south-1_DU27AHJvZ` |
| Cognito region | `ap-south-1` |
| Cognito app client id | `1d6rgvitjsfoonlkbm07uivgmg` |
| Username form | `<user>@po_uat` |
| Subject types | `Pump` (the primary entity) and `PaymentRateChart` |

The `Pump` subject type has **no programs**, so a pump's "Completed visits" (the
self-report surveys, grievance forms and payment-rate changes) are **general
encounters**, not program encounters — see [Data model](#data-model-entities--how-they-link).

None of the credentials above are secrets except the password and the token; the
server URL, org shortname, pool id, region and client id are public configuration
that the mobile app ships anyway, and they are necessary context for debugging a
wrong-environment token.

---

## Authentication (read this first — it explains the 500s)

Avni uses **AWS Cognito**. Every API call needs the Cognito **ID token** (a JWT)
in the `auth-token` header (no `Bearer ` prefix):

```
auth-token: <cognito id token>        # the ID token, NOT the access token
```

### Method (a): grab the id token from the browser

Log into `app.avniproject.org`, open DevTools → **Console**, and run:

```js
copy(Object.entries(localStorage).find(([k]) => k.endsWith('.idToken'))[1])
```

This copies the **id** token to your clipboard. In Local Storage the entries are
stored by the Cognito SDK as `CognitoIdentityServiceProvider.<clientId>.<user>.idToken`
(also `.accessToken`, `.refreshToken`, `.LastAuthUser`, `.clockDrift`). **Use `.idToken`.**
Then:

```bash
export AVNI_AUTH_TOKEN="$(pbpaste)"
```

### Method (b): programmatic Cognito login (`USER_PASSWORD_AUTH`)

Instead of pasting a token, log in via Cognito with boto3 (`pip install boto3`).
Set the credentials as env vars and the code will fetch the id token for you:

```bash
export AVNI_USERNAME="<user>@po_uat"
export AVNI_PASSWORD="<password>"
export AVNI_COGNITO_CLIENT_ID="1d6rgvitjsfoonlkbm07uivgmg"   # our app client id
export AVNI_COGNITO_REGION="ap-south-1"
# AVNI_AUTH_TOKEN unset -> the code falls back to Cognito login
```

Keep these in a local `.env` that is never committed. The client id / region
**must belong to the same instance** as `AVNI_SERVER`. If Cognito returns a
`ChallengeName` (e.g. forced password reset), log in once via the web app first,
then retry.

### The 500 trap (important — this is why we were stuck)

The server verifies the JWT's `iss` (Cognito pool/region), `aud` (client id) and
`token_use` (must be `id`). The exception mapping is:

| Token problem | HTTP result |
|---|---|
| Expired | 401 |
| Malformed / not a JWT | 401 |
| **Wrong issuer / audience / `token_use=access` / bad signature** | **500** |

So a bare **HTTP 500 from `/api/...` is almost always the token**, not your
request: either you grabbed the **access token** instead of the id token, or the
token was minted for a **different Avni environment** than `AVNI_SERVER`. The
request shape, env vars, and date format are not the cause. (Confirmed: an
anonymous call to `/api/subjects` returns 401, never 500.) See the
[debugging history](#debugging-history--root-cause-writeup) for the full trace.

### Decode a token to check it

```bash
python3 -c "import os,base64,json,time; t=os.environ['AVNI_AUTH_TOKEN']; p=t.split('.')[1]; p+='='*(-len(p)%4); c=json.loads(base64.urlsafe_b64decode(p)); print('iss',c.get('iss')); print('aud',c.get('aud')); print('token_use',c.get('token_use')); print('expired',c.get('exp',0)<time.time())"
```

Expect `token_use = id` and `expired = False`. Our token's `iss` pool is
`ap-south-1_DU27AHJvZ` and `aud` (client id) is `1d6rgvitjsfoonlkbm07uivgmg`.

Running `python3 2-code/20-programs/avni_auth.py` prints exactly these claims and
then makes one small live call — the fastest way to tell a bad token from a bad
request. It is a manual check, not an automatic one: the only token check the
client makes before a call is `exp` (it refreshes an expired token), and
`pull.py` decodes the token only at the end of a pull, to record `token_user` in
`_manifest.json`.

Id tokens **expire in ~1 hour** — after that you get **401**; re-grab (method a)
or let `AvniClient.token()` refresh (method b). A full-org pull can outlive a
static token, which is why method (b) is preferred for long runs.

---

## Environment variables

These seven are the **only** environment variables the code reads. `Main.py`
loads them from a repo-root `.env` (see `.env.example`); a real environment
variable always wins over the file.

| Var | Meaning | Default | Read by |
|---|---|---|---|
| `AVNI_SERVER` | Base URL; must match the token's environment | `https://app.avniproject.org` | `avni_auth.py` |
| `AVNI_AUTH_TOKEN` | Cognito **id** token (auth method a) | — | `avni_auth.py` |
| `AVNI_USERNAME` | Cognito username, `<user>@po_uat` (auth method b) | — | `avni_auth.py` |
| `AVNI_PASSWORD` | Cognito password (auth method b) | — | `avni_auth.py` |
| `AVNI_COGNITO_CLIENT_ID` | Cognito app client id | `1d6rgvitjsfoonlkbm07uivgmg` | `avni_auth.py` |
| `AVNI_COGNITO_REGION` | Cognito region | `ap-south-1` | `avni_auth.py` |
| `AVNI_BOX_ROOT` | Local Box Drive mount to publish into | (a default path) | `sync_box.py` |

The Cognito credential vars must belong to the **same instance** as `AVNI_SERVER`.
Only `AVNI_AUTH_TOKEN` and `AVNI_PASSWORD` are secrets; keep them in `.env`,
which is git-ignored.

**Everything else is a CLI flag or a code default, not an env var.** The API
`version` is an `AvniClient` constructor argument (default `2`); the subject-type
filter and the incremental cursor are `--subject-type` and `--since` on
`avni_to_tables.py`, whose cursor default is the module constant `DEFAULT_SINCE`
(`1900-01-01T00:00:00.001Z`); `includeCatchments` is supported by the API but
never passed. There is **no page cap**: `get_all_pages()` walks until a page
comes back empty, so a pull is never silently truncated.

---

## Two layers: metadata vs data

Avni has two conceptual layers. Keeping them straight is the key to reading the API.

- **Metadata layer** (the "schema"): definitions configured in the Admin app —
  **Subject Types** (Pump, PaymentRateChart, …), **Programs**, **Encounter Types**,
  **Forms** (Form → Form Element Groups → Form Elements), and **Concepts**.
  A **Concept** is the atomic field (dataType Text/Numeric/Date/Coded; Coded
  concepts define their allowed answers). The keys inside every `observations{}`
  object are **concept names**.
- **Data layer**: the actual records returned by the API (subjects, enrolments,
  encounters). Their `observations{}` are `{ conceptName: value }` maps.

To see the metadata authoritatively: the Admin app on `app.avniproject.org`
(Subject Types / Programs / Encounter Types / Forms / Concepts), or the DIL
Part A Admin & Program Manager Guide (not kept in this repo) — that guide *is*
the metadata/config (schema) layer. To see it empirically: dump a record, since
its `observations` keys are the live concept names.

Because concept names are the join between the API and our columns, renaming a
concept in the Admin app silently changes the API payload. The concept→column map
lives in `2-code/20-programs/avni_pipeline/concepts.py`, and any concept it does
not know still lands in `observations_long.csv` with an empty `concept_column`
and is counted in the quality report — nothing is dropped silently.

---

## Data model (entities & how they link)

```
Subject                         ID, External ID, "Subject type"  (e.g. "Pump")
│  observations{}               form data: { conceptName: value }
│  location{}, "Location ID"
│
├─ relatives[]                  SubjectRelationship -> "Relative ID" = another Subject
├─ encounters[]                 uuids of GENERAL Encounters (visits, no program)
└─ enrolments[]                 uuids of ProgramEnrolments
       │
       ProgramEnrolment         ID, "Subject ID", "Program"
       │  observations{}, exitObservations{}
       └─ encounters[]          uuids of ProgramEncounters
              │
              ProgramEncounter  ID, "Enrolment ID", "Subject ID", "Encounter type"
                 observations{}

General Encounter               ID, "Subject ID", "Encounter type"   (directly on subject)
   observations{}
```

A subject owns three kinds of children: **relatives**, **general encounters**
(standalone visits), and **program enrolments** (which own **program encounters**).
For the **Pump** subject type there are no programs, so its visits are **general
encounters** and `enrolments[]` is empty. The pipeline still walks the program
path generically, so it keeps working if a program is added later.

### Join keys

| From → To | Match on |
|---|---|
| Enrolment → Subject | `"Subject ID"` == subject `"ID"` (or `"Subject external ID"` == `"External ID"`) |
| ProgramEncounter → Enrolment | `"Enrolment ID"` == enrolment `"ID"` |
| Encounter / ProgramEncounter → Subject | `"Subject ID"` == subject `"ID"` |
| Subject → children | subject `enrolments[]` / `encounters[]` arrays hold the child uuids |

The link is **bidirectional/redundant**: the parent lists child uuids *and* each
child carries the parent id. For a sync, pull each resource from its own endpoint
and stitch by `Subject ID` / `Enrolment ID`.

> **The attribution rule you must not regress.** The server **ignores**
> `/api/encounters?subjectId=` and returns org-wide encounters. Attribute every
> encounter by its **own `"Subject ID"`**, client-side, with the subject
> document's `encounters[]` uuid list only as a fallback. `pull.py` does this and
> records which path was used in the `attribution` column of `encounters.csv`.

### "Completed visits" screen → API mapping

The web app's visit table (Visit Name / Scheduled / Completed / Cancel) is just
encounters. For a Pump these are **general encounters** (`GET /api/encounters`):

| UI column | API field (Encounter / ProgramEncounter) |
|---|---|
| Visit Name | `"Encounter type"` (the form name) |
| Visit Scheduled Date | `"Earliest scheduled date"` |
| Visit Completed Date | `"Encounter date time"` |
| Visit Cancel Date | `"Cancel datetime"` |

An encounter with a scheduled date but no `Encounter date time` is a *planned*
(not yet done) visit; with `Encounter date time` set it is *completed*; with
`Cancel datetime` set it is *cancelled*. That three-way split is the `status`
column downstream.

---

## Endpoints we use

All under `https://app.avniproject.org`. `auth-token` header required on all.
`version` is a **query param** (not a header). Reads are paginated (see
[Pagination & versioning](#pagination--versioning)).

| Method | Path | Purpose | Key params |
|---|---|---|---|
| GET | `/api/subjects` | list subjects | `lastModifiedDateTime`*, `subjectType`, `includeCatchments`, `concepts`, `locationIds`, `version` |
| GET | `/api/subject/{ID}` | one subject (full, incl. `enrolments[]`/`encounters[]`) | `includeCatchments`, `version` — `{ID}` is uuid **or** External ID |
| GET | `/api/encounters` | general (subject) encounters | `subjectId` (**uuid**, but see the attribution rule), `encounterType`, `lastModifiedDateTime`, `concepts`, `version` |
| GET | `/api/encounter/{ID}` | one general encounter | `version` |
| GET | `/api/programEnrolments` | program enrolments | `lastModifiedDateTime`†, `program`, `subject` (**External ID**), `version` |
| GET | `/api/programEnrolment/{ID}` | one enrolment | `version` |
| GET | `/api/programEncounters` | program encounters | `lastModifiedDateTime`*, `programEnrolmentId`, `encounterType`, `version` |
| GET | `/api/programEncounter/{ID}` | one program encounter | `version` |
| GET | `/api/groupSubjects` | group membership | `version` |
| GET | `/api/locations`, `/api/location/{ID}` | address hierarchy | `version` |
| GET | `/media/signedUrl` | presign one private media URL | `url` (the stored S3 URL) |
| POST/PUT/PATCH/DELETE | `/api/subject`, `/api/programEnrolment`, `/api/programEncounter`, `/api/encounter` | write (idempotent by External ID) | — |

`*` required. `†` required *unless* both `subject` and `program` are given.
This repo only ever reads; the write endpoints are listed for completeness.

### The uuid-vs-External-ID quirk (easy to get wrong)

- `/api/encounters?subjectId=` wants the subject's **uuid** (`"ID"`) — and is
  ignored by the server anyway, so we page all encounters once and join locally.
- `/api/programEnrolments?subject=` wants the subject's **External ID** *and* also
  needs `program`. To list a subject's enrolments without those, read the
  subject's `enrolments[]` uuids and `GET /api/programEnrolment/{uuid}` each
  (this is what `pull.py` does).

---

## Pagination & versioning

- **Cursor pagination.** Pages default to ~20–100 items. To get the next page,
  re-request with `lastModifiedDateTime` = the **last record's**
  `audit."Last modified at"`. Stop when a page returns empty `content` (that is
  the real end-of-data signal).
- **Do not trust `totalElements`.** Avni fills that field with the *per-page*
  count, not the grand total. Use the empty-page signal. (`totalPages`/`pageSize`
  are also per-request.)
- **Version** (`version` query param): `1` = ISO datetimes for user dates;
  `2` = `yyyy-MM-dd` for user-intended dates (recommended, our default). Audit
  timestamps and the `lastModifiedDateTime` request param stay full ISO
  (`yyyy-MM-dd'T'HH:mm:ss.SSSZ`) in all versions.

---

## Entity field reference

**Subject**: `ID`, `External ID`, `Voided`, `Subject type`, `Registration date`,
`Registration location`, `location{}` (full hierarchy), `Location ID`, `relatives[]`,
`observations{}`, `encounters[]`, `enrolments[]`, `audit`.

**ProgramEnrolment**: `ID`, `External ID`, `Voided`, `Subject ID`, `Subject external ID`,
`Subject type`, `Program`, `Enrolment datetime`, `Enrolment location`, `Exit datetime`,
`Exit location`, `observations{}`, `exitObservations{}`, `encounters[]`, `audit`.

**ProgramEncounter**: `ID`, `External ID`, `Voided`, `Subject ID`, `Subject external ID`,
`Subject type`, `Enrolment ID`, `Enrolment external ID`, `Program`, `Encounter type`,
`Encounter location`, `Encounter date time`, `Earliest scheduled date`,
`Max scheduled date`, `observations{}`, `Cancel location`, `Cancel datetime`,
`cancelObservations{}`, `audit`.

**Encounter** (general): same as ProgramEncounter minus the `Enrolment*`/`Program` fields.

**SubjectRelationship**: `Voided`, `Relationship type`, `Relative ID`,
`Relative external ID`, `Enter date`, `Exit date`.

**Audit**: `Created at`, `Last modified at`, `Created by`, `Last modified by`.
`Created by` is the operator username and is how `operators.csv` exists at all —
there is no user endpoint.

**ResponsePage** (list wrapper): `content[]`, `totalElements` (per-page, see
[Pagination](#pagination--versioning)), `totalPages`, `pageSize`.

---

## Media / field photos

Photo answers (cartridge, water sample, damaged part, new tablets) are stored as
**private S3 URLs** in the `prod-user-media` bucket. A bare GET returns **403**:
the bucket denies anonymous reads, and the URL in the CSV is an identifier, not a
download link.

To fetch the bytes, presign each URL through Avni with the same `auth-token`
header: `GET /media/signedUrl?url=<stored s3 url>` returns a short-lived
presigned URL, which is then downloaded.
`2-code/20-programs/avni_pipeline/media.py` does this, and
`2-code/21-wrangling/fetch_media.py` is the standalone CLI over it.

Presigned URLs **expire** — treat them as ephemeral and never write them into the
analysis tables. The CSVs keep the stable S3 URL; the local copies are recorded
in `media/_media_manifest.csv` (`s3_url` → local path, bytes, sha256), and only
`report.html` is rewritten to relative `media/` paths so a shared folder renders
without credentials.

---

## Code that talks to this source

| File | What it does |
|---|---|
| `2-code/20-programs/avni_auth.py` | Reusable **`AvniClient`**: auto Cognito login + token refresh + authenticated GET / pagination helpers. The only auth path — nothing else should hand-roll `urllib`. |
| `2-code/20-programs/avni_pipeline/pull.py` | API reads via `AvniClient`: subjects, general encounters, enrolments, program encounters. Does the **client-side** subject↔encounter join. |
| `2-code/20-programs/avni_pipeline/concepts.py` | Concept / EAV handling: typing, multi-select expansion, one-hot indicators, concept→column map. |
| `2-code/20-programs/avni_pipeline/transform.py` | Raw records → tidy analysis tables. Voided records become `*_voided` tables rather than being dropped. |
| `2-code/20-programs/avni_pipeline/emit_csv.py` | Writes the table set to CSV (and reads it back), plus the data dictionary, manifest and quality report. |
| `2-code/20-programs/avni_pipeline/report.py` | Builds the self-contained interactive HTML report (no CDN, no external assets). |
| `2-code/20-programs/avni_pipeline/media.py` | Resolves the private S3 photo URLs through `GET /media/signedUrl` and downloads them. |
| `2-code/21-wrangling/avni_to_tables.py` | **Pipeline entry point.** Avni API → tidy CSV tables → HTML report. Three sources: `api`, `raw`, `csv`. |
| `2-code/21-wrangling/fetch_media.py` | Standalone CLI over `media.py`: localise the photos for an output directory that already exists, and repoint the report at them. |
| `2-code/21-wrangling/sync_box.py` | Standalone CLI: publish an output directory (and its raw snapshot) to Box. **The only place Box paths appear.** |

The three sources of `avni_to_tables.py`: `api` hits the live API, `raw` replays a
saved `_raw.json` snapshot, and `csv` re-renders the report from tables already on
disk. Only `api` needs credentials.

### `avni_auth.py` — the reusable client

`AvniClient` handles both auth methods above, refreshes an expired id token by
re-running Cognito login, and exposes GET + pagination helpers so scripts don't
re-implement the `auth-token` header and cursor walk. **Needs boto3 for the
Cognito path** (`pip install boto3`).

```python
from avni_auth import AvniClient

client = AvniClient(
    server=...,        # falls back to env AVNI_SERVER (default https://app.avniproject.org)
    username=...,      # falls back to env AVNI_USERNAME
    password=...,      # falls back to env AVNI_PASSWORD
    client_id=...,     # falls back to env AVNI_COGNITO_CLIENT_ID (default 1d6rgvitjsfoonlkbm07uivgmg)
    region=...,        # falls back to env AVNI_COGNITO_REGION (default ap-south-1)
    auth_token=...,    # falls back to env AVNI_AUTH_TOKEN (a pre-fetched id token)
    version="2",
)

client.token()                        # valid id token; auto Cognito login + refresh on expiry
client.login()                        # force a fresh Cognito USER_PASSWORD_AUTH login
client.get(path, **params)            # authenticated GET -> parsed JSON
client.get_all_pages(path, **params)  # walk cursor pagination -> list of all items
```

If `auth_token` (or `AVNI_AUTH_TOKEN`) is provided, the client uses it directly;
if it is missing or expired, it falls back to `USER_PASSWORD_AUTH` with the
Cognito credentials.

### Running a pull

```bash
export AVNI_SERVER="https://app.avniproject.org"
export AVNI_AUTH_TOKEN="$(pbpaste)"        # after the copy(...) snippet above

# via the repo entry point
python3 Main.py tables --out data/ --subject-type Pump

# or calling the script directly
python3 2-code/21-wrangling/avni_to_tables.py --out data/                      # all subject types
python3 2-code/21-wrangling/avni_to_tables.py --out data/ --subject-type Pump  # just pumps

python3 2-code/21-wrangling/avni_to_tables.py --source raw --input data/_raw.json --out data/
python3 2-code/21-wrangling/avni_to_tables.py --source csv --input data/       # re-render HTML only
python3 2-code/21-wrangling/avni_to_tables.py --out data/ --fetch-media        # + localise the photos
```

Narrow with `--subject-type` to speed a pull up. Add `--save-raw` to keep the
snapshot the run was built from — that snapshot is the only reproducible record
of a pull, since the API only ever shows "now".

`--fetch-media` downloads the field photos into `<out>/media/` and points the
report at the local copies. It runs **after** the CSVs and the report are on disk
— that is where the photo URLs are read from and where the rewrite happens — and
an auth failure there is reported without losing the tables (the run just exits
non-zero; `fetch_media.py --dir <out>` retries).

### Publishing to Box

```bash
python3 2-code/21-wrangling/sync_box.py --dir data/ --raw data/_raw.json   # --dry-run first
```

| Box folder | Rule |
|---|---|
| `12-avni/120-raw/pull_<UTC>.json` | **Append-only.** A pull can never be reproduced, so a snapshot is never overwritten; a same-named file with different bytes is refused, not replaced. |
| `12-avni/121-clean/` | **Overwritten every run.** Everything in it is derived from a snapshot by code; the `_manifest.json` inside records which pull and which options produced it. |

Box paths live *only* in `sync_box.py`. `avni_pipeline/` stays
transport-agnostic: the pipeline writes a local directory and knows nothing about
where it is published, so replacing Box with S3 or a data repo is a second
standalone script rather than an edit to `emit_csv.py`. Copies go via a `.part`
file inside the destination and are renamed into place, because Box syncs
whatever appears in the folder — including a half-written `report.html`.

---

## Debugging history / root-cause writeup

The work started from a **500 Internal Server Error** on every call to the Avni
API. The investigation and fix, for the record:

1. **Symptom**: `GET /api/subjects` returned a bare **HTTP 500** — no useful body.
2. **Ruled out the request**: reading the avni-server source
   (`AuthenticationFilter` → `AuthService` → `BaseIAMService.verifyAndDecodeToken`)
   showed the request shape, params, and date format were already correct. The 500
   was coming out of **token verification**, before the controller ran.
3. **Root cause**: we were sending the Cognito **access token** instead of the
   **id token**. Avni verifies the JWT's `iss` / `aud` / `token_use` (must be
   `id`). A token that fails those checks throws a `JWTVerificationException`,
   which the server re-throws as a **bare HTTP 500** — whereas an *expired* or
   *malformed* token gives **401**. So the 500 specifically pointed at "valid JWT,
   wrong token/environment", not at our request.
4. **Fix**: grab the **`.idToken`** from browser localStorage
   (`copy(Object.entries(localStorage).find(([k]) => k.endsWith('.idToken'))[1])`),
   or log in programmatically and use the Cognito `IdToken`.
5. **Result**: listing worked immediately — the first successful pull returned
   both subject types across several pages.

This is why `avni_auth.py` has a self-check that prints the token claims
(`python3 2-code/20-programs/avni_auth.py`), why `pull.py` records the token's
user in `_manifest.json`, and why the scripts print a "500 is almost always the
token" hint on failure.

A second, subtler bug came from the server-side `?subjectId=` filter, which is
accepted and then ignored: it returns org-wide encounters, so a naive
per-subject loop attributed every encounter to every subject. The fix is the
client-side join described under [Join keys](#join-keys).

---

## Gotchas checklist

- **500 from any endpoint** → wrong token (access vs id) or wrong environment.
  See [Authentication](#authentication-read-this-first--it-explains-the-500s).
- **401** → token missing/expired (id tokens last ~1 hour) → re-grab.
- **403** → authenticated but lacks the `user` role / ViewSubject privilege.
- **403 on a photo URL** → expected; the media bucket denies anonymous reads.
  Presign through `GET /media/signedUrl`.
- **Empty results, 200** → a `subjectType`/name that doesn't exist returns empty,
  not an error.
- **`totalElements` is per-page**, not a total. Page until `content` is empty.
- **Use the id token**, header name `auth-token`, value has no `Bearer ` prefix.
- **`/api/encounters?subjectId=` is ignored** — join client-side on the
  encounter's own `Subject ID`.
- **Voided records are returned** — treat `Voided: true` as deleted in a sync.
  They land in `_voided/*.csv` rather than being dropped.
- **`version` is a query param**, not a header.
- **uuid vs External ID**: `/api/encounters?subjectId=` wants the **uuid**;
  `/api/programEnrolments?subject=` wants the **External ID** (+ `program`);
  otherwise walk `enrolments[]`.
- **Concept names are the schema.** Renaming a concept in the Admin app changes
  the payload keys; the unmapped-concept count in `_quality_report.md` is the
  early warning.

---

## Next steps

Heading toward an **incremental sync**:

- **Persist the cursor** — save the last `audit."Last modified at"` per resource
  so each run only pulls changes since the previous run (`--since` is the manual
  equivalent today).
- **Handle voided records** — treat `Voided: true` as a delete/tombstone in the
  local store rather than an upsert.
- **Stitch by id keys** — pull each resource from its own endpoint and join by
  `Subject ID` / `Enrolment ID` (as `pull.py` does) rather than relying on nested
  arrays.
- **Possible local Avni MCP server** — expose these read helpers as an MCP server
  so an agent can query pumps and visits directly.
