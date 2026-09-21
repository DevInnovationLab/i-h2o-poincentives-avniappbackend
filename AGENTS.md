# AGENTS.md

Authoritative conventions for this repository. Read this before writing code
here, whether you are a person or an agent. Where this file and any other
document disagree, this file wins.

**This repository is public.** See [Data privacy](#data-privacy) before you
commit anything, open an issue, or attach a file.

## What this repo is

The backend for the i-h2o POIncentives study (in-line chlorination devices on
village water pumps, Odisha and Andhra Pradesh, India). It pulls pump-operator
data from the Avni platform API, transforms it into tidy CSV tables, renders an
interactive HTML report, downloads the field photos, and publishes the result
to Box.

It produces analysis *inputs*. It does no analysis.

## Folder map

| Path | Contents |
|---|---|
| `Main.py` | Single entry point. `tables` / `media` / `box` sub-commands. |
| `1-data/12-avni/` | Local mirror of the Box data folder. Fully gitignored. |
| `1-data/12-avni/120-raw/` | Append-only raw API snapshots, `pull_<UTC>.json`. |
| `1-data/12-avni/121-clean/` | Derived CSV tables, report, `media/`. Rewritten each run. |
| `2-code/20-programs/` | Reusable code: `avni_auth.py`, `envfile.py`, `avni_pipeline/`. **The import root.** |
| `2-code/21-wrangling/` | The three CLI programs: `avni_to_tables.py`, `fetch_media.py`, `sync_box.py`. |
| `3-output/31-notebooks/` | Exploratory notebooks. Nothing here is an input to anything. |
| `4-documentation/40-data-sources/` | What Avni is, how the API behaves, how to authenticate. |
| `4-documentation/41-data-dictionaries/` | Column-level documentation of the output tables. |
| `4-documentation/42-design/` | Design notes and the historical build spec. |
| `.context/` | Agent-facing checklists (`check-repo.md`). |

Each top-level numbered folder (`1-data/`, `2-code/`, `3-output/`,
`4-documentation/`) carries its own `README.md`. Each `1-data/` subfolder
carries a sidecar `.md` named after itself. Numbered *sub*folders are described
by their parent's README rather than carrying one each.

## The import contract

**`2-code/20-programs/` is the import root.** Entry scripts add it to
`sys.path`; nothing else may.

- `Main.py` and the three programs in `2-code/21-wrangling/` are entry scripts.
  They may `sys.path.insert` the import root.
- Modules inside `avni_pipeline/` may not. Never add `sys.path.insert` inside
  `avni_pipeline/`.
- **One legacy exception:** `avni_pipeline/pull.py` inserts its parent
  directory so that `from avni_auth import AvniClient` works when the file is
  run script-style rather than imported as part of the package. It predates
  this rule. Do not copy the pattern into new modules, and do not delete it
  without checking that script-style execution still works.

The literal string `"20-programs"` appears in several of these bootstraps
(`Main.py`, the entry scripts). **Renumbering or renaming the folder breaks
imports** in places a search for the folder name will find but a search for a
Python identifier will not. If you must renumber, grep for `20-programs`
across the whole repo first.

## Naming

- Python modules and packages: `snake_case.py`. No numeric prefixes on code
  files; the folders carry the numbering, the modules carry meaning.
- Documentation sidecars in `1-data/` are named after their folder:
  `12-avni/12-avni.md`, `120-raw/120-raw.md`.
- **CSV table names are load-bearing code constants, not filenames you may
  tidy.** `subjects.csv`, `encounters.csv`, `fact_self_report_survey.csv`,
  `fact_grievance.csv`, `fact_payment_rate.csv`, `observations_long.csv`,
  `operators.csv` are read back by name from `emit_csv.ANALYSIS_TABLES` (and
  its `VOIDED_TABLES` sibling). They must **not** be renamed into numbered
  form (`121-subjects.csv` and friends). Renaming them silently breaks the
  `--source csv` re-render path, the manifest and the data dictionary.
- Branches: kebab-case, branched from `main`. See `CONTRIBUTING.md`.

## Data privacy

This repo is public. The data it moves is not.

1. **Nothing under `1-data/` is ever committed.** The `.gitignore` allows only
   directories, `*.md` sidecars and `.gitkeep` there. If you find yourself
   using `git add -f` under `1-data/`, stop.
2. **`report.html` is data, not output.** It embeds the entire dataset as
   inline JSON: real village names, operator usernames, free-text grievances.
   It must never be committed, never be attached to an issue, and never be
   pasted into a chat or a PR. `*.html` is denied globally in `.gitignore` for
   exactly this reason.
3. **`data_dictionary.csv` is not safe either.** Its `example` column is
   populated from live rows, so the "documentation" table carries real values.
   `*.csv` is denied globally.
4. **Never paste a token anywhere persistent.** Not in an issue, not in a
   commit message, not in a settings file, not in a test fixture. Tokens live
   in `.env` (gitignored) or in the shell for one command. If a token reaches
   a commit, treat it as leaked: rotate it, do not just amend.
5. **Field photos are data.** They live under `121-clean/media/` on Box, never
   in the repo.
6. **Village names, operator usernames and free-text answers are real.** Do
   not use them in examples, test fixtures or documentation -- invent values
   that are obviously synthetic. `.context/check-repo.md` lists the specific
   strings known to have leaked, and scans for them.

   The one thing that is *not* covered by this rule is the Avni
   **organisation** name, which is a configuration identifier rather than a
   person or a place: documentation has to name it for anyone to point the
   client at the right instance. Individual login identifiers built from it
   are still off limits.

## Deliberate deviations from the DIL convention

These are intentional. Do not "fix" them; if you disagree, change this section
first and say why.

- **No `21-` instrument-code mirroring.** The DIL convention mirrors survey
  instrument code under `21-`. There is exactly one source here (the Avni API)
  and no instrument to index, so `21-wrangling/` holds the wrangling programs
  instead.
- **No `22-hfcs/`.** This repo does no high-frequency checks. The pipeline
  emits `_quality_report.md` alongside the tables; that is the whole of the
  QC surface.
- **No `24-analysis/`.** This repo produces analysis inputs. Analysis lives in
  the study repo, not here.
- **No numbered table filenames.** See [Naming](#naming) — the CSV names are
  code constants.
- **No R scaffolding.** No `.Rprofile`, no `pacman`, no `.Rproj`, no `Main.R`.
  This codebase is 100% Python and standard-library-only by design (`boto3` is
  the single optional dependency, for the Cognito login path). `Main.py` plays
  `Main.R`'s role; `2-code/20-programs/envfile.py` plays `.Rprofile`'s
  auto-load-`.env` role.
- **`4-documentation/42-design/` is an extension** to the reference folder set,
  for design notes and the historical build spec.
- **`*.html` and `*.csv` are denied globally in `.gitignore`**, where the
  reference allows them under `3-output/**`. Both are record-level data here.
  The reasoning is written into `.gitignore` itself.

## Before you commit

Run the checklists in [`.context/check-repo.md`](.context/check-repo.md). They
are short, they are concrete, and the secret/PII scans in them are the reason
this repo can be public at all.
