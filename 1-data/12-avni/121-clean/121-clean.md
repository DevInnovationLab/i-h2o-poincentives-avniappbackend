# 121-clean

The derived outputs: tidy CSV tables, their documentation, the HTML report and
the localised field photos. **Rewritten on every run.**

Everything here is produced from a snapshot in `120-raw/` by code, so it is
reproducible and disposable. The `_manifest.json` written alongside the tables
records which pull and which options produced this particular build. Nothing
here is ever hand-edited: if a value is wrong, the fix is in the code.

## Table inventory

Grains only — this file documents shape, never values. Column-level
documentation lives in `4-documentation/41-data-dictionaries/`.

| File | Grain |
|---|---|
| `subjects.csv` | one row per pump |
| `encounters.csv` | one row per encounter |
| `fact_self_report_survey.csv` | one row per self-report survey encounter |
| `fact_grievance.csv` | one row per grievance encounter |
| `fact_payment_rate.csv` | one row per payment rate encounter |
| `observations_long.csv` | one row per (encounter, concept) — the tidy EAV backbone |
| `operators.csv` | one row per submitting user, derived from encounter audit fields |
| `media/` | one file per localised field photo, plus a manifest joining S3 URL to local path |

`encounters_wide.csv` is written only when the pipeline is run with
`--big-table`: one row per encounter with every fact table joined on. It is a
convenience denormalisation, never a source of truth.

The `fact_*` tables are the form-specific facts, one per study form, keyed back
to `encounters.csv`. `observations_long.csv` is the same information in atomic
form and is the fallback when a concept has no column of its own.

Voided records are written to a `_voided/` subdirectory rather than dropped, so
"it disappeared" is always answerable.

## Encoding rules

The same rules apply to every table, and they are recorded in
`_manifest.json` so a reader never has to guess:

- **Booleans** are `1` / `0`, never `TRUE`/`FALSE`, never `yes`/`no`.
- **Missing** is an empty cell. Not `NA`, not `NULL`, not `-99`, not `0`.
- **Datetimes** are ISO 8601 in **UTC**, with the `Z` suffix. Dates are plain
  ISO 8601.
- **Multi-valued answers** are pipe-joined in a single cell (`a|b|c`).
- **Ids are always strings**, never numbers. They are UUIDs; anything that
  reads them as numeric will corrupt them.

## The HTML report is a data file

`report.html` is **not** a summary or a rendered artifact. It embeds the entire
dataset as inline JSON so that the viewer works offline — real village names,
operator usernames, free-text grievances, photo references. It is data with a
user interface attached.

Treat it exactly as you would treat the CSVs: never commit it, never attach it
to an issue, never share the file outside the project's Box folder. `*.html` is
denied globally in `.gitignore` for this reason alone. See
`AGENTS.md > Data privacy`.

## Box path

```
<AVNI_BOX_ROOT>/121-clean/
```

`Main.py box` publishes this whole directory there, overwriting the previous
build. That is safe precisely because it is reproducible — unlike `120-raw/`,
which is append-only.
